"""Reachability classification (spec 2026-08-09, Phases 1-2).

Pure core: decides whether a session's phone link is up, and whether its
reported activity permits restarting it. No I/O — the adapter samples
Claude Code's own per-process state file, this module only judges. Mirrors
`crr.core.takeover.ready_to_take_over`'s shape.
"""

import pytest

from crr.core import reachability as r


# --- reachability() -------------------------------------------------------

def test_a_live_bridge_session_id_is_reachable():
    assert r.reachability("session_013C", pid_matched=True, field_present=True) == "reachable"


def test_a_null_bridge_session_id_is_unreachable():
    assert r.reachability(None, pid_matched=True, field_present=True) == "unreachable"


def test_an_empty_string_is_unreachable_not_reachable():
    # Falsy but present: Claude Code writes null, but a "" would otherwise
    # sneak through a truthiness check as a live session id.
    assert r.reachability("", pid_matched=True, field_present=True) == "unreachable"


def test_a_pid_that_does_not_match_is_unknown():
    # 117 of 133 state files on the author's machine belong to dead pids,
    # and 2 to RECYCLED pids now owned by unrelated processes. One session
    # had three files, two with "alive" pids. Liveness alone lies.
    assert r.reachability("session_013C", pid_matched=False, field_present=True) == "unknown"


def test_a_missing_bridge_field_is_unknown_not_unreachable():
    # An older Claude Code, or a renamed field. Absence of the field is not
    # evidence the bridge is down.
    assert r.reachability(None, pid_matched=True, field_present=False) == "unknown"


def test_pid_mismatch_wins_over_everything():
    assert r.reachability(None, pid_matched=False, field_present=False) == "unknown"


# --- may_kick() -----------------------------------------------------------

@pytest.mark.parametrize("status", ["busy", "shell"])
def test_work_in_flight_is_never_kicked(status):
    allowed, reason = r.may_kick(status)
    assert allowed is False
    assert status in reason


def test_idle_may_be_kicked():
    allowed, _ = r.may_kick("idle")
    assert allowed is True


def test_waiting_may_be_kicked():
    # The deadlock-breaker. A session blocked on a permission prompt never
    # reaches a clean assistant-end turn boundary, so a boundary-only guard
    # would refuse forever exactly the session that most needs unsticking —
    # blocked on a question the user cannot answer, because the phone is
    # disconnected.
    allowed, _ = r.may_kick("waiting")
    assert allowed is True


@pytest.mark.parametrize("status", [None, "", "some-future-status"])
def test_an_unrecognised_status_is_never_kicked(status):
    allowed, reason = r.may_kick(status)
    assert allowed is False
    assert "unknown" in reason.lower() or "unrecognised" in reason.lower()


def test_idle_and_waiting_are_the_only_permitted_statuses():
    permitted = {s for s in ("idle", "busy", "shell", "waiting") if r.may_kick(s)[0]}
    assert permitted == {"idle", "waiting"}
