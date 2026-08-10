"""Per-sid kick-attempt history for the dropped-Remote-Control watchdog
(review fix-wave 2026-08-07, FIX 1 — CRITICAL).

Without this, `cli._kick_dropped_bridges` is stateless across sweeps: a
failed reconnect (host briefly offline, auth expired, RC unavailable)
re-qualifies for a kick on every 30s pass forever, because a kick does not
itself make the session reachable and it stays LIVE at a clean turn boundary.
This module is the guard: a cooldown (never re-kick the same sid within
`bridge_kick_cooldown_seconds`) and a hard attempt cap
(`bridge_kick_max_attempts`), with the counter reset ONLY by a confirmed
`reachable` reading — never by a timer, or the cap just delays the loop.

Same JSON-in-the-state-dir / atomic-write discipline as
`crr/core/exclusions.py` and `crr/core/settings.py`.
"""

import pytest

from crr.core import bridge_kicks

_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_SID2 = "11112222-3333-4444-5555-666677778888"


# --------------------------------------------------------------------------
# kick_eligible — the pure cooldown/cap decision.
# --------------------------------------------------------------------------

def test_first_ever_attempt_is_eligible():
    ok, reason = bridge_kicks.kick_eligible(
        attempts=0, last_kick_ts=None, now=1000.0,
        cooldown_seconds=600, max_attempts=3,
    )
    assert ok is True
    assert reason is None


def test_within_cooldown_is_refused_with_a_reason():
    ok, reason = bridge_kicks.kick_eligible(
        attempts=1, last_kick_ts=1000.0, now=1200.0,  # 200s since last kick
        cooldown_seconds=600, max_attempts=3,
    )
    assert ok is False
    assert reason is not None and "cooldown" in reason.lower()


def test_exactly_at_cooldown_boundary_is_eligible():
    ok, _reason = bridge_kicks.kick_eligible(
        attempts=1, last_kick_ts=1000.0, now=1600.0,  # exactly 600s later
        cooldown_seconds=600, max_attempts=3,
    )
    assert ok is True


def test_past_cooldown_is_eligible_again():
    ok, reason = bridge_kicks.kick_eligible(
        attempts=1, last_kick_ts=1000.0, now=1601.0,
        cooldown_seconds=600, max_attempts=3,
    )
    assert ok is True
    assert reason is None


def test_at_max_attempts_is_refused_with_a_stated_reason_even_past_cooldown():
    # Past the cap, the cooldown having elapsed does not matter — the cap
    # is a hard stop until a confirmed "ok" resets it.
    ok, reason = bridge_kicks.kick_eligible(
        attempts=3, last_kick_ts=1000.0, now=999_999.0,
        cooldown_seconds=600, max_attempts=3,
    )
    assert ok is False
    assert reason is not None and ("cap" in reason.lower() or "attempt" in reason.lower())


def test_below_max_attempts_and_past_cooldown_is_eligible():
    ok, _reason = bridge_kicks.kick_eligible(
        attempts=2, last_kick_ts=1000.0, now=1601.0,
        cooldown_seconds=600, max_attempts=3,
    )
    assert ok is True


# --------------------------------------------------------------------------
# KickHistoryStore — round-trip, degrade-closed, bounded.
# --------------------------------------------------------------------------

def test_store_unseen_sid_has_zero_attempts_and_no_last_kick(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.attempts(_SID) == 0
    assert store.last_kick_ts(_SID) is None


def test_store_record_kick_increments_attempts_and_stamps_time(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(_SID, 1000.0)
    assert store.attempts(_SID) == 1
    assert store.last_kick_ts(_SID) == 1000.0
    store.record_kick(_SID, 1700.0)
    assert store.attempts(_SID) == 2
    assert store.last_kick_ts(_SID) == 1700.0


def test_store_record_kick_persists_across_instances(tmp_path):
    bridge_kicks.KickHistoryStore(tmp_path).record_kick(_SID, 1000.0)
    fresh = bridge_kicks.KickHistoryStore(tmp_path)
    assert fresh.attempts(_SID) == 1
    assert fresh.last_kick_ts(_SID) == 1000.0


def test_store_reset_clears_the_sid_back_to_unseen(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(_SID, 1000.0)
    store.reset(_SID)
    assert store.attempts(_SID) == 0
    assert store.last_kick_ts(_SID) is None


def test_store_reset_of_unseen_sid_is_a_harmless_no_op(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.reset(_SID)  # must not raise
    assert store.attempts(_SID) == 0


def test_store_tracks_multiple_sids_independently(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(_SID, 1000.0)
    store.record_kick(_SID2, 2000.0)
    store.record_kick(_SID2, 2500.0)
    assert store.attempts(_SID) == 1
    assert store.attempts(_SID2) == 2
    store.reset(_SID)
    assert store.attempts(_SID) == 0
    assert store.attempts(_SID2) == 2  # untouched


def test_store_missing_file_is_not_degraded(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.is_degraded() is False


def test_store_corrupt_file_reports_degraded(tmp_path):
    (tmp_path / bridge_kicks.FILENAME).write_text("{not json", encoding="utf-8")
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.is_degraded() is True


def test_store_wrong_shape_reports_degraded(tmp_path):
    (tmp_path / bridge_kicks.FILENAME).write_text('["nope"]', encoding="utf-8")
    assert bridge_kicks.KickHistoryStore(tmp_path).is_degraded() is True


def test_store_degraded_read_reports_zero_attempts_not_a_raise(tmp_path):
    # Fail CLOSED matches the watchdog's own gate (it refuses the whole step
    # while degraded) — but the store's own reads must not raise either way.
    (tmp_path / bridge_kicks.FILENAME).write_text("{not json", encoding="utf-8")
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.attempts(_SID) == 0
    assert store.last_kick_ts(_SID) is None


def test_store_bounded_evicts_the_least_recently_kicked_sid(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    for i in range(bridge_kicks.MAX_ENTRIES):
        sid = f"{i:08x}-0000-4000-8000-000000000000"
        store.record_kick(sid, float(i))
    oldest_sid = "00000000-0000-4000-8000-000000000000"
    assert store.attempts(oldest_sid) == 1

    newcomer = "ffffffff-0000-4000-8000-000000000000"
    store.record_kick(newcomer, float(bridge_kicks.MAX_ENTRIES))

    assert store.attempts(newcomer) == 1
    assert store.attempts(oldest_sid) == 0  # evicted to make room
