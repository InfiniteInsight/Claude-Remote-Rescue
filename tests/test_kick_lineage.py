"""Kick-attempt lineage (#35 — audit run-3 P8, State-first lineage).

`bridge_kicks.record_kick` stored `{attempts, last_kick_ts}` and nothing
else — for an action that SIGTERMs a live `claude`. After the fact you
could not reconstruct WHY a session was restarted three times: not the
observation that justified it, not the pid signalled, not the outcome, not
the thresholds in force.

"Store the conditions that produced an output, not just the conclusion. An
output you cannot regenerate from recorded inputs is a claim you cannot
audit." Sibling practice exists and was itself a prior audit fix:
ArchiveStore records reason + archived_at + cwd for every archived entry.

The counters keep working exactly as before — the cooldown and the cap read
`attempts`/`last_kick_ts` unchanged. Lineage is additive.
"""

import json

from crr.core import bridge_kicks, contracts

SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _obs(**over):
    base = {"pid": 4242, "bridge_since": 812, "bridge_seen": True,
            "stale_after": 150, "cooldown_seconds": 600, "max_attempts": 3,
            "config_defaults_version": contracts.SESSIONS_CONTRACT_VERSION}
    base.update(over)
    return base


def test_record_kick_stores_the_justifying_observation(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    entry = store.last_attempt(SID)
    assert entry["pid"] == 4242
    assert entry["bridge_since"] == 812
    assert entry["stale_after"] == 150
    assert entry["at"] == 1000.0


def test_the_thresholds_in_force_are_recorded_not_assumed(tmp_path):
    # A threshold change later must not silently rewrite the history of
    # decisions taken under the old one.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs(stale_after=999, max_attempts=7))
    entry = store.last_attempt(SID)
    assert entry["stale_after"] == 999
    assert entry["max_attempts"] == 7


def test_the_outcome_is_recordable_after_the_kick_returns(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    store.record_outcome(SID, ok=False, message="no such process")
    entry = store.last_attempt(SID)
    assert entry["outcome_ok"] is False
    assert "no such process" in entry["outcome"]


def test_outcome_before_any_attempt_is_a_harmless_noop(tmp_path):
    # The kick runs inside a try/finally; a failure path could reach here
    # with nothing recorded. It must not raise.
    bridge_kicks.KickHistoryStore(tmp_path).record_outcome(SID, ok=True, message="x")


def test_history_keeps_the_last_few_attempts_not_just_the_newest(tmp_path):
    # "Why was this restarted 3 times?" needs all three, not the last one.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    for i in range(3):
        store.record_kick(SID, 1000.0 + i, observation=_obs(bridge_since=100 * i))
    attempts = store.attempt_log(SID)
    assert [a["bridge_since"] for a in attempts] == [0, 100, 200]


def test_the_attempt_log_is_bounded(tmp_path):
    # Lineage must not become an unbounded append log in a file consulted on
    # every watchdog sweep.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    for i in range(bridge_kicks.MAX_ATTEMPT_LOG + 5):
        store.record_kick(SID, float(i), observation=_obs())
    assert len(store.attempt_log(SID)) == bridge_kicks.MAX_ATTEMPT_LOG


def test_counters_still_work_exactly_as_before(tmp_path):
    # The cooldown/cap guard reads these two; lineage is additive and must
    # not disturb them.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    store.record_kick(SID, 2000.0, observation=_obs())
    assert store.attempts(SID) == 2
    assert store.last_kick_ts(SID) == 2000.0
    store.reset(SID)
    assert store.attempts(SID) == 0
    assert store.last_kick_ts(SID) is None
    # The log is NOT cleared (#45): reset means "the bridge came back", and
    # erasing the record of the kicks that got it back is exactly what made
    # `crr kicks --list` useless for every successful case.
    assert len(store.attempt_log(SID)) == 2


def test_a_legacy_counter_only_file_still_reads(tmp_path):
    # Files written before this change carry no attempt log.
    (tmp_path / bridge_kicks.FILENAME).write_text(json.dumps(
        {"sessions": {SID: {"attempts": 2, "last_kick_ts": 500.0}}}), encoding="utf-8")
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.is_degraded() is False
    assert store.attempts(SID) == 2
    assert store.attempt_log(SID) == []
    assert store.last_attempt(SID) is None


# --- end to end: a real watchdog kick records real lineage, and a human
#     can read it back ---------------------------------------------------

def test_a_watchdog_kick_records_its_justification(tmp_path, capsys):
    from crr import cli
    from crr.adapters import session_state
    from crr.core import config as cfg, settings
    from crr.core.journal import JournalStore, new_entry
    from crr.core.ops import OpResult

    entry = new_entry(pid=4242, cwd="/home/u/p", host="tab", shell="bash",
                      boot_id="boot-1", now="2026-01-01T00:00:00+00:00",
                      tmux_session=None,
                      claude={"session_id": SID, "sid_source": "injected",
                              "started": "2026-01-01T00:00:00+00:00"})
    store = JournalStore(tmp_path)
    store.write(entry)

    class FakeBoot:
        def current(self): return "boot-1"

    class FakeProbe:
        def is_alive(self, pid): return True
        def has_controlling_tty(self, pid): return True
        def controlling_ttys(self, pids): return set(pids)

    class FakeController:
        """`claude_groups` supplies `pid_matched`: the state file's pid must
        be one of this shell's live claude jobs."""
        def claude_groups(self, shell_pid): return [5150]

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(),
        settings.SettingsStore(tmp_path), store, tmp_path,
        controller=FakeController(), flags=None,
        read_session_state=lambda: {SID: session_state.SessionState(
            pid=5150, bridge_session_id=None, field_present=True,
            status="idle", waiting_for="")},
        read_takeover_signal=lambda s: {"mtime": 0.0, "tail_kind": "assistant-end"},
        kick=lambda *a, **k: OpResult(True, "kicked 4242"),
        clock=lambda: 10_000.0,
    )

    attempt = bridge_kicks.KickHistoryStore(tmp_path).last_attempt(SID)
    assert attempt is not None, "the kick recorded no lineage at all"
    assert attempt["pid"] == 4242
    # What justified it, under the reachability detector (spec 2026-08-09):
    # Claude Code's own null bridgeSessionId, read off a state file whose
    # pid matched a live claude here. There is no `stale_after` any more —
    # the old record pinned that threshold because changing it later would
    # rewrite the history of every decision taken under the old value, and
    # reachability has no such tunable.
    assert attempt["reachability"] == "unreachable"
    assert attempt["bridge_session_id"] is None
    assert attempt["field_present"] is True
    assert attempt["pid_matched"] is True
    assert attempt["status"] == "idle"
    assert attempt["max_attempts"] == 3            # the thresholds in force
    assert attempt["outcome_ok"] is True
    assert "kicked 4242" in attempt["outcome"]


def test_crr_kicks_list_reads_the_lineage_back(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import state_dir

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1_700_000_000.0, observation=_obs())
    store.record_outcome(SID, ok=False, message="no such process")

    assert cli.main(["kicks", "--list"]) == 0
    out = capsys.readouterr().out
    assert SID[:8] in out
    assert "812" in out            # the observation
    assert "150" in out            # the threshold
    assert "FAILED" in out
    assert "no such process" in out


def test_crr_kicks_list_renders_a_reachability_observation(tmp_path, monkeypatch, capsys):
    # The read path is where lineage earns its keep, and the detector's
    # vocabulary changed under it (spec 2026-08-09, Phase 3). Rendering a
    # reachability record through the old record-counting format printed
    # "? records since the bridge marker (threshold ?)" — a sentence that
    # is both blank and false, for the one command a human runs to find out
    # why their session was restarted.
    from crr import cli
    from crr.adapters import state_dir

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1_700_000_000.0, observation={
        "pid": 4242, "state_pid": 5150, "pid_matched": True,
        "bridge_session_id": None, "field_present": True,
        "reachability": "unreachable", "status": "waiting",
        "waiting_for": "permission prompt",
        "cooldown_seconds": 600, "max_attempts": 3,
        "config_defaults_version": 14,
    })
    store.record_outcome(SID, ok=True, message="kicked 4242")

    assert cli.main(["kicks", "--list"]) == 0
    out = capsys.readouterr().out
    assert "unreachable" in out                  # what justified it
    assert "waiting" in out                      # the activity that permitted it
    assert "permission prompt" in out            # what it was blocked on
    assert "kicked 4242" in out
    assert "?" not in out, "the new lineage rendered as unknown fields"


def test_crr_kicks_list_still_reads_a_legacy_record_counting_record(tmp_path, monkeypatch, capsys):
    # Records written by the old detector are still on disk in every
    # installed copy — the log is bounded, not migrated. They must keep
    # rendering in their own vocabulary rather than degrading to "?".
    from crr import cli
    from crr.adapters import state_dir

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1_700_000_000.0, observation=_obs())
    store.record_outcome(SID, ok=True, message="kicked 4242")

    assert cli.main(["kicks", "--list"]) == 0
    out = capsys.readouterr().out
    assert "812" in out and "150" in out
    assert "bridge marker" in out


def test_crr_kicks_list_says_so_when_there_is_nothing(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import state_dir

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    assert cli.main(["kicks", "--list"]) == 0
    assert "no auto-kick attempts" in capsys.readouterr().out


# --- what the LIVE run of #45 found --------------------------------------
# The first end-to-end kick against a real process worked: the claude group
# died, the shim relaunched on the same conversation, and the lineage was
# written. Then the real 30s watchdog fired, saw the reconnect, and called
# reset() — which deleted the whole entry, log and all.
#
# So the audit trail survived only when a kick FAILED. The successful case —
# the common one, and the one you most want to be able to explain later —
# erased itself. reset() must clear the COUNTERS (what the cooldown and cap
# read) without destroying the record of what happened.

def test_reset_clears_the_counters(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    store.record_kick(SID, 2000.0, observation=_obs())
    store.reset(SID, now=3000.0)
    assert store.attempts(SID) == 0
    assert store.last_kick_ts(SID) is None


def test_reset_keeps_the_lineage(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs(bridge_since=812))
    store.record_outcome(SID, ok=True, message="kicked 2049505")
    store.reset(SID, now=3000.0)
    log = store.attempt_log(SID)
    assert any(a.get("bridge_since") == 812 for a in log), \
        "the kick that succeeded erased its own record"
    assert any(a.get("outcome") == "kicked 2049505" for a in log)


def test_reset_records_the_reconnect_itself(tmp_path):
    # The reconnect is the END of the story — without it the log stops at
    # "kicked" and never says whether it worked.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    store.reset(SID, now=3000.0)
    assert store.attempt_log(SID)[-1] == {"at": 3000.0, "event": "reconnected"}


def test_reset_on_a_session_with_no_history_is_still_a_noop(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.reset(SID, now=1.0)
    assert store.attempt_log(SID) == []
    assert store.attempts(SID) == 0


def test_a_reset_entry_does_not_break_lru_eviction(tmp_path):
    # reset() leaves last_kick_ts as None. The eviction key used to be
    # `.get("last_kick_ts", 0)`, which mixes None with floats and raises
    # TypeError the moment the map fills up — a latent crash the live run
    # would not have reached for another 499 sessions.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    for i in range(bridge_kicks.MAX_ENTRIES + 2):
        sid = f"{i:08x}-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
        store.record_kick(sid, float(i), observation=_obs())
        if i % 2 == 0:
            store.reset(sid, now=float(i))
    assert len(store.session_ids()) <= bridge_kicks.MAX_ENTRIES


def test_reset_still_lets_a_later_drop_be_kicked_immediately(tmp_path):
    # A confirmed reconnect means the next drop is a NEW incident, not a
    # continuation — so neither the cap nor the cooldown may carry over.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    for i in range(3):
        store.record_kick(SID, 1000.0 + i, observation=_obs())
    store.reset(SID, now=2000.0)
    eligible, reason = bridge_kicks.kick_eligible(
        attempts=store.attempts(SID), last_kick_ts=store.last_kick_ts(SID),
        now=2001.0, cooldown_seconds=600, max_attempts=3)
    assert eligible is True, reason


# --- the SECOND thing the live run found ---------------------------------
# reset() is called on EVERY sweep where the bridge reads "ok", not just on
# the transition. Appending a "reconnected" record each time meant one entry
# per 30s watchdog pass, and within ~3 minutes the bounded log held five
# identical reconnect records and had evicted the kick that caused them.
# Observed live: the real kick record was gone inside 3 minutes.

def test_repeated_resets_append_only_one_reconnect(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    for t in range(2000, 2600, 30):          # 20 sweeps, all seeing "ok"
        store.reset(SID, now=float(t))
    log = store.attempt_log(SID)
    assert [a.get("event", "KICK") for a in log] == ["KICK", "reconnected"]


def test_the_kick_record_survives_a_long_healthy_period(tmp_path):
    # The regression, stated as its consequence: after an hour of healthy
    # sweeps you can still answer "why was this restarted?".
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs(bridge_since=812))
    store.record_outcome(SID, ok=True, message="kicked 2049505")
    for t in range(2000, 2000 + 120 * 30, 30):   # 120 sweeps ~= 1 hour
        store.reset(SID, now=float(t))
    assert any(a.get("bridge_since") == 812 for a in store.attempt_log(SID))


def test_a_later_drop_starts_a_new_incident_in_the_log(tmp_path):
    # kick -> reconnect -> kick again must read as two incidents, not one.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs(bridge_since=200))
    store.reset(SID, now=2000.0)
    store.reset(SID, now=2030.0)                 # healthy sweep, no-op
    store.record_kick(SID, 3000.0, observation=_obs(bridge_since=300))
    store.reset(SID, now=4000.0)
    assert [a.get("event", "KICK") for a in store.attempt_log(SID)] == [
        "KICK", "reconnected", "KICK", "reconnected"]


# --- the transition counter (plan 2026-08-10, Task 7) --------------------
#
# The spec ships with one honest gap: Claude Code never persists
# `replBridgeError` (all 8 writers to its state file were enumerated), so a
# bridge that comes up and then errors WITHOUT running teardown leaves a
# stale session id on disk and the detector reads `reachable`. The miss is
# silent but safe — no kick, never a wrong kick, and manual `crr kick` still
# works.
#
# These counters are what make that gap countable rather than a story. If
# crr observes not one reachable -> unreachable transition over a week while
# the user watches `/rc` vanish on their own terminal, the assumption behind
# the detector is disproven and there is a number to say so with.
#
# Top-level keys, not per-sid: the question is "does this detector ever fire
# at all", not "which session".

SID2 = "11112222-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_an_observed_reachable_to_unreachable_transition_is_counted(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_transition(SID, now=1000.0)
    assert store.observed_transitions() == 1


def test_transitions_accumulate_across_sweeps(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    for t in (1000.0, 2000.0):
        store.record_transition(SID, now=t)
    assert store.observed_transitions() == 2
    assert store.last_transition_at() == 2000.0


def test_a_legacy_file_reports_zero_transitions(tmp_path):
    (tmp_path / bridge_kicks.FILENAME).write_text('{"sessions": {}}', encoding="utf-8")
    assert bridge_kicks.KickHistoryStore(tmp_path).observed_transitions() == 0


def test_a_store_that_has_never_seen_a_transition_says_so(tmp_path):
    # `None`, not 0.0 — "never" and "at the epoch" are different claims.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.observed_transitions() == 0
    assert store.last_transition_at() is None


def test_a_transition_counts_are_shared_not_per_sid(tmp_path):
    # Two different sessions dropping is two observations of the SAME
    # question ("does the detector fire?"), so one counter, not two.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_transition(SID, now=1000.0)
    store.record_transition(SID2, now=1100.0)
    assert store.observed_transitions() == 2


def test_a_corrupt_counter_reads_as_zero_rather_than_raising(tmp_path):
    # An observability counter must never be able to take the watchdog down.
    (tmp_path / bridge_kicks.FILENAME).write_text(
        '{"sessions": {}, "observed_transitions": "lots", "last_transition_at": true}',
        encoding="utf-8")
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.is_degraded() is False    # not a reason to stop kicking
    assert store.observed_transitions() == 0
    assert store.last_transition_at() is None


# --- the writer/reader trap, which this store has already sprung once ----
# Every existing writer ends `write_json_atomic(path, {"v": .., "sessions":
# ..})` — it REBUILDS the document rather than updating it. A new top-level
# key is therefore erased by the next kick, and `reset()` runs on every
# sweep that sees a healthy bridge, so the erasure lands within ~30s. The
# mirror image is worse: a counter write that drops `sessions` erases
# `attempts`/`last_kick_ts` and reopens the restart loop FIX 1 closed.

def test_the_transition_count_survives_a_later_kick(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_transition(SID, now=1000.0)
    store.record_kick(SID, 1001.0, observation=_obs())
    assert store.observed_transitions() == 1
    assert store.last_transition_at() == 1000.0


def test_the_transition_count_survives_the_every_sweep_reset(tmp_path):
    # `reset()` is the dangerous one: it fires on EVERY healthy sweep, so a
    # counter it clobbers reads a permanent zero — the exact false disproof
    # this counter exists to prevent.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    store.record_transition(SID, now=1001.0)
    for t in range(2000, 2600, 30):
        store.reset(SID, now=float(t))
    assert store.observed_transitions() == 1


def test_recording_a_transition_does_not_erase_the_kick_counters(tmp_path):
    # The cooldown and the attempt cap ARE the restart-loop protection.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs(bridge_since=812))
    store.record_transition(SID, now=1001.0)
    assert store.attempts(SID) == 1
    assert store.last_kick_ts(SID) == 1000.0
    assert store.attempt_log(SID)[-1]["bridge_since"] == 812


def test_record_outcome_does_not_erase_the_transition_count(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    store.record_transition(SID, now=1001.0)
    store.record_outcome(SID, ok=True, message="kicked 4242")
    assert store.observed_transitions() == 1


# --- the previous-reachability memory ------------------------------------
# `crr revive` is a systemd ONESHOT — a fresh process every 30s. A
# transition is a comparison between two sweeps, so the previous reading
# has to outlive the process that took it, or the count is a permanent zero
# indistinguishable from the failure mode being measured.

def test_the_last_reading_is_remembered_across_instances(tmp_path):
    bridge_kicks.KickHistoryStore(tmp_path).remember_reachability(SID, "reachable")
    assert bridge_kicks.KickHistoryStore(tmp_path).last_reachability(SID) == "reachable"


def test_an_unseen_sid_has_no_remembered_reading(tmp_path):
    assert bridge_kicks.KickHistoryStore(tmp_path).last_reachability(SID) is None


def test_the_memory_is_per_sid(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.remember_reachability(SID, "reachable")
    store.remember_reachability(SID2, "unreachable")
    assert store.last_reachability(SID) == "reachable"
    assert store.last_reachability(SID2) == "unreachable"


def test_the_memory_does_not_disturb_the_kick_history(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(SID, 1000.0, observation=_obs())
    store.remember_reachability(SID, "unreachable")
    assert store.attempts(SID) == 1
    assert store.last_kick_ts(SID) == 1000.0


def test_remembering_a_reading_does_not_invent_kick_history(tmp_path):
    # `crr kicks --list` walks `session_ids()`. If the memory lived inside
    # `sessions`, every live session crr merely LOOKED at would render as
    # "0 attempt(s) since the last confirmed reconnect / no lineage
    # recorded" — kick history for a session that was never kicked.
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.remember_reachability(SID, "reachable")
    assert store.session_ids() == []


def test_the_memory_is_bounded_like_the_rest_of_the_file(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    for i in range(bridge_kicks.MAX_ENTRIES + 5):
        store.remember_reachability(f"{i:08x}-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "reachable")
    raw = json.loads((tmp_path / bridge_kicks.FILENAME).read_text(encoding="utf-8"))
    assert len(raw["reachability_seen"]) <= bridge_kicks.MAX_ENTRIES
    # The newest reading survives; the oldest is what gets dropped.
    newest = f"{bridge_kicks.MAX_ENTRIES + 4:08x}-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    assert store.last_reachability(newest) == "reachable"


def test_a_legacy_file_has_no_memory_and_does_not_raise(tmp_path):
    (tmp_path / bridge_kicks.FILENAME).write_text('{"sessions": {}}', encoding="utf-8")
    assert bridge_kicks.KickHistoryStore(tmp_path).last_reachability(SID) is None


# --- end to end: the watchdog sweep is what has to do the counting -------

def _transition_sweep(tmp_path, states, entries=1, clock_at=10_000.0):
    """Run one real `_kick_dropped_bridges` pass over `entries` journaled
    shells sharing SID, with `read_session_state` returning `states`."""
    from crr import cli
    from crr.core import config as cfg, settings
    from crr.core.journal import JournalStore, new_entry
    from crr.core.ops import OpResult

    store = JournalStore(tmp_path)
    for i in range(entries):
        store.write(new_entry(
            pid=4242 + i, cwd="/home/u/p", host="tab", shell="bash",
            boot_id="boot-1", now="2026-01-01T00:00:00+00:00", tmux_session=None,
            claude={"session_id": SID, "sid_source": "injected",
                    "started": "2026-01-01T00:00:00+00:00"}))

    class FakeBoot:
        def current(self): return "boot-1"

    class FakeProbe:
        def is_alive(self, pid): return True
        def has_controlling_tty(self, pid): return True
        def controlling_ttys(self, pids): return set(pids)

    class FakeController:
        def claude_groups(self, shell_pid): return [5150]

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(),
        settings.SettingsStore(tmp_path), store, tmp_path,
        controller=FakeController(), flags=None,
        read_session_state=lambda: states,
        read_takeover_signal=lambda s: {"mtime": 0.0, "tail_kind": "assistant-end"},
        kick=lambda *a, **k: OpResult(True, "kicked 4242"),
        clock=lambda: clock_at,
    )


def _state(bridge_session_id, *, field_present=True, status="idle"):
    from crr.adapters import session_state
    return {SID: session_state.SessionState(
        pid=5150, bridge_session_id=bridge_session_id,
        field_present=field_present, status=status, waiting_for="")}


def test_a_sweep_that_sees_a_bridge_drop_counts_one_transition(tmp_path, capsys):
    _transition_sweep(tmp_path, _state("session_013C"), clock_at=10_000.0)
    _transition_sweep(tmp_path, _state(None), clock_at=10_030.0)
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.observed_transitions() == 1
    assert store.last_transition_at() == 10_030.0


def test_a_bridge_that_was_never_reachable_counts_nothing(tmp_path, capsys):
    # The FIRST reading of a session is not a transition: crr has no
    # evidence the bridge was ever up, and claiming one would inflate the
    # very number that is supposed to test the detector.
    _transition_sweep(tmp_path, _state(None), clock_at=10_000.0)
    assert bridge_kicks.KickHistoryStore(tmp_path).observed_transitions() == 0


def test_a_bridge_that_stays_down_counts_one_transition_not_one_per_sweep(tmp_path, capsys):
    _transition_sweep(tmp_path, _state("session_013C"), clock_at=10_000.0)
    for i in range(1, 6):
        _transition_sweep(tmp_path, _state(None), clock_at=10_000.0 + 30 * i)
    assert bridge_kicks.KickHistoryStore(tmp_path).observed_transitions() == 1


def test_an_unknown_reading_between_them_does_not_lose_the_transition(tmp_path, capsys):
    # `unknown` is the ABSENCE of a reading (no state file, a stale pid, an
    # unparseable field). Overwriting the memory with it would mean the
    # drop that follows is invisible — the last thing crr actually KNEW was
    # "reachable", so the drop is still a drop.
    _transition_sweep(tmp_path, _state("session_013C"), clock_at=10_000.0)
    _transition_sweep(tmp_path, _state(None, field_present=False), clock_at=10_030.0)
    _transition_sweep(tmp_path, _state(None), clock_at=10_060.0)
    assert bridge_kicks.KickHistoryStore(tmp_path).observed_transitions() == 1


def test_a_reconnect_makes_the_next_drop_a_new_transition(tmp_path, capsys):
    _transition_sweep(tmp_path, _state("session_013C"), clock_at=10_000.0)
    _transition_sweep(tmp_path, _state(None), clock_at=10_030.0)
    _transition_sweep(tmp_path, _state("session_013C"), clock_at=10_060.0)
    _transition_sweep(tmp_path, _state(None), clock_at=10_090.0)
    assert bridge_kicks.KickHistoryStore(tmp_path).observed_transitions() == 2


def test_two_journal_entries_sharing_a_sid_count_one_transition(tmp_path, capsys):
    # The duplicate_group case. `kicked_sids` only dedupes AFTER a kick
    # attempt, so both entries reach the detector on every sweep.
    _transition_sweep(tmp_path, _state("session_013C"), entries=2, clock_at=10_000.0)
    _transition_sweep(tmp_path, _state(None), entries=2, clock_at=10_030.0)
    assert bridge_kicks.KickHistoryStore(tmp_path).observed_transitions() == 1


def test_a_drop_is_counted_even_when_the_kick_is_refused(tmp_path, capsys):
    # The question is "does the DETECTOR fire", not "did crr kick". A
    # session generating (`busy`) is refused by `may_kick`, and counting
    # only the kicks would report zero on a machine where every drop
    # happened mid-turn.
    _transition_sweep(tmp_path, _state("session_013C", status="busy"), clock_at=10_000.0)
    _transition_sweep(tmp_path, _state(None, status="busy"), clock_at=10_030.0)
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.observed_transitions() == 1
    assert store.session_ids() == []           # nothing was kicked
    assert "busy" in capsys.readouterr().out   # and it said why


def test_the_sweep_does_not_break_the_kick_history_it_shares_a_file_with(tmp_path, capsys):
    # The writer/reader trap, exercised against what `cli` ACTUALLY writes
    # rather than a hand-built fixture: one real sweep that both counts a
    # transition and records a kick, read back through `crr kicks --list`.
    from crr import cli
    from crr.adapters import state_dir

    _transition_sweep(tmp_path, _state("session_013C"), clock_at=10_000.0)
    _transition_sweep(tmp_path, _state(None), clock_at=10_030.0)

    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.observed_transitions() == 1
    assert store.attempts(SID) == 1                    # the kick still counted
    assert store.last_kick_ts(SID) == 10_030.0         # the cooldown still has its clock

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(state_dir, "state_dir", lambda: tmp_path)
        assert cli.main(["kicks", "--list"]) == 0
    out = capsys.readouterr().out
    assert SID[:8] in out
    assert "unreachable" in out
    assert "kicked 4242" in out
    assert "?" not in out


# --- crr doctor is where a human reads the number ------------------------

def test_doctor_reports_the_observed_transition_count(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import state_dir

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    bridge_kicks.KickHistoryStore(tmp_path).record_transition(SID, now=1_700_000_000.0)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "reachable->unreachable" in out
    assert "1 " in out
    assert "2023-11-14" in out          # the stamp, not just the count


def test_doctor_says_none_observed_rather_than_printing_a_bare_zero(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import state_dir

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "reachable->unreachable" in out
    assert "none observed yet" in out


def test_doctor_does_not_print_an_unreadable_count_as_zero(tmp_path, monkeypatch, capsys):
    # Zero is a CLAIM here — it is the evidence that would disprove the
    # detector. An unreadable file must say "unknown", never "0".
    from crr import cli
    from crr.adapters import state_dir

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / bridge_kicks.FILENAME).write_text("{not json", encoding="utf-8")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "unreadable" in out
    assert "0 reachable->unreachable" not in out
