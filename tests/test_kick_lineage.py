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
