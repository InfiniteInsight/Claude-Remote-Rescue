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
    assert store.attempt_log(SID) == []


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

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(),
        settings.SettingsStore(tmp_path), store, tmp_path,
        controller=None, flags=None,
        read_tail_facts=lambda s, cap, **kw: {
            "last_prompt": "", "model": "", "last_active": "", "last_reply": "",
            "title": "", "slug": "", "transcript_bytes": 0,
            "bridge_seen": True, "bridge_since": 812},
        read_takeover_signal=lambda s: {"mtime": 0.0, "tail_kind": "assistant-end"},
        kick=lambda *a, **k: OpResult(True, "kicked 4242"),
        clock=lambda: 10_000.0,
    )

    attempt = bridge_kicks.KickHistoryStore(tmp_path).last_attempt(SID)
    assert attempt is not None, "the kick recorded no lineage at all"
    assert attempt["pid"] == 4242
    assert attempt["bridge_since"] == 812          # what justified it
    assert attempt["stale_after"] == 150           # the threshold in force
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
