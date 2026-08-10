"""The unreachable-Remote-Control watchdog step (spec 2026-08-09, Phase 3).

`cli._kick_dropped_bridges` is the separate, LIVE-acting pass appended to
`crr revive` after the existing crashed-session revival. Driven entirely by
fakes: a fake boot/probe (mirrors test_reviver.py's shape) decide the
classifier state, a fake controller reports which claude process groups are
alive under the journaled shell, and `read_session_state` /
`read_takeover_signal` / `kick` are injected so no real state file, process
or transcript is ever touched. Every test asserts on the fake `kick` being
called (or not) — never on a real process.

The detector's source changed in this phase: it no longer counts transcript
records looking for a bridge marker (which never fired on an idle session,
because an idle session writes none). It reads Claude Code's own
`bridgeSessionId` out of `~/.claude/sessions/<pid>.json` via
`adapters.session_state.read_all`, and classifies with
`core.reachability`.
"""

import pytest

from crr import cli
from crr.adapters import session_state
from crr.core import bridge_kicks
from crr.core import config as cfg
from crr.core import settings
from crr.core.journal import JournalStore, new_entry
from crr.core.ops import OpResult

_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_BOOT = "current-boot-0000"
_PID = 4242          # the journaled SHELL pid
_CLAUDE_PID = 5150   # the live claude job running under that shell


class FakeBoot:
    def current(self):
        return _BOOT


class FakeProbe:
    """A LIVE-classifying probe by default; flip either flag for other states."""

    def __init__(self, alive=True, tty=True):
        self._alive = alive
        self._tty = tty

    def is_alive(self, pid):
        return self._alive

    def has_controlling_tty(self, pid):
        return self._tty


class FakeController:
    """Stands in for `PsProcessController`.

    `claude_groups(shell_pid)` is the only method this step calls: it
    supplies `pid_matched`, the check that the state file describes THIS
    session's live claude rather than a dead or recycled pid. Default: one
    live claude job whose group id matches the state file's pid.
    """

    def __init__(self, groups=(_CLAUDE_PID,)):
        self._groups = list(groups)
        self.asked = []

    def claude_groups(self, shell_pid):
        self.asked.append(shell_pid)
        return list(self._groups)


class _Recorder:
    """Stands in for `ops.kick` — records calls, never touches a process."""

    def __init__(self, ok=True):
        self.calls = []
        self._ok = ok

    def __call__(self, store, controller, flags, boot, probe, pid, *, grace):
        self.calls.append(pid)
        return OpResult(self._ok, f"kicked {pid} (resuming the same conversation)")


def _entry(pid=_PID, boot=_BOOT, sid=_SID):
    return new_entry(
        pid=pid, cwd="/home/u/project", host="tmux", shell="zsh",
        boot_id=boot, now="2026-08-07T00:00:00Z",
        claude={"session_id": sid, "sid_source": "injected", "started": "2026-08-07T00:00:00Z"},
    )


def _state(bridge_session_id=None, *, field_present=True, status="idle",
           waiting_for="", pid=_CLAUDE_PID, sid=_SID):
    """One `{sid: SessionState}` map, exactly as `session_state.read_all`
    returns it. The default is the actionable case: the field is readable
    and null — Claude Code itself saying the phone link is down."""
    return {sid: session_state.SessionState(
        pid=pid, bridge_session_id=bridge_session_id,
        field_present=field_present, status=status, waiting_for=waiting_for,
    )}


def _signal(tail_kind="assistant-end", mtime=0.0):
    return {"mtime": mtime, "tail_kind": tail_kind}


def _run(entries, settings_store, tmp_path, recorder, *, now=10_000.0,
         state=None, signal=None, probe=None, controller=None,
         config=None, store=None, kick_store=None):
    """One `_kick_dropped_bridges` pass with every seam faked."""
    return cli._kick_dropped_bridges(
        entries, FakeBoot(), probe or FakeProbe(), config or cfg.Config(),
        settings_store, store or JournalStore(tmp_path), tmp_path,
        controller if controller is not None else FakeController(), None,
        read_session_state=lambda: _state() if state is None else state,
        read_takeover_signal=lambda sid: signal or _signal(),
        kick=recorder, clock=lambda: now, kick_store=kick_store,
    )


# --------------------------------------------------------------------------
# Detection: reachability decides, and only `unreachable` is actionable.
# --------------------------------------------------------------------------

def test_an_unreachable_idle_session_is_kicked(tmp_path, capsys):
    # THE case the old record-counting detector could never reach: idle, so
    # it wrote no transcript records and the bridge-marker counter never
    # advanced past its threshold. Claude Code's own state file says the
    # link is down regardless of whether anything is being written.
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         state=_state(None, status="idle"))

    assert recorder.calls == [_PID]
    assert "8a1b2c3d" in capsys.readouterr().out


def test_an_unreachable_waiting_session_is_kicked_despite_a_mid_turn_tail(tmp_path):
    # THE deadlock-breaker, and the reason this test forces `mid-turn`.
    #
    # A session blocked on a permission prompt never reaches a clean
    # assistant-end boundary — its tail is mid-turn for as long as it stays
    # blocked. A blanket `ready_to_take_over` gate would therefore refuse
    # this session FOREVER: stuck on a question its owner cannot answer,
    # because the phone that would answer it is disconnected.
    #
    # If someone reinstates the boundary check for every status, this test
    # fails. Asserting on a clean `assistant-end` signal instead would
    # prove nothing, because such a session would pass either way.
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         state=_state(None, status="waiting", waiting_for="permission prompt"),
         signal=_signal(tail_kind="mid-turn"))

    assert recorder.calls == [_PID]


def test_an_unreachable_idle_session_still_needs_a_clean_boundary(tmp_path, capsys):
    # The other half of the asymmetry above. `idle` is a completed turn, so
    # a mid-turn tail contradicts it — two independent signals must agree
    # before this step signals a live process. `waiting` gets no such
    # corroboration because none is obtainable (see the test above).
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         state=_state(None, status="idle"), signal=_signal(tail_kind="mid-turn"))

    assert recorder.calls == []
    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "mid-turn" in out


@pytest.mark.parametrize("status", ["busy", "shell"])
def test_an_unreachable_but_working_session_is_never_kicked(tmp_path, status):
    # `busy` is claude generating; `shell` is a command running under it.
    # Kicking either destroys work in flight, and an unreachable phone is
    # not worth that.
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         state=_state(None, status=status))

    assert recorder.calls == []


@pytest.mark.parametrize("status", [None, "", "some-future-status"])
def test_an_unrecognised_status_is_never_kicked(tmp_path, status):
    # A status a future Claude Code invents is not a licence to signal a
    # live process.
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         state=_state(None, status=status))

    assert recorder.calls == []


def test_a_reachable_session_is_never_kicked(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         state=_state("session_013C", status="idle"))

    assert recorder.calls == []


@pytest.mark.parametrize("state,why", [
    ({}, "no state file for this session at all"),
    (_state(None, pid=999_999), "the state file's pid is not a live claude here"),
    (_state(None, pid=None), "the state file's pid was unreadable"),
    (_state(None, field_present=False), "no bridgeSessionId key — an older Claude Code"),
    (_state(None, field_present=False, status="waiting"), "unreadable field, blocked session"),
])
def test_an_unknown_reachability_is_never_kicked(tmp_path, state, why):
    # Absence of evidence. Every one of these routes lands on "unknown",
    # and acting on it would SIGTERM a live process on the strength of
    # something crr could not read. `field_present=False` also covers the
    # case Task 2 fixed: a `bridgeSessionId` that is neither string nor
    # null (a future Claude Code reshaping it) must not read as "down".
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         state=state)

    assert recorder.calls == [], why


def test_pid_matching_asks_the_controller_about_the_journaled_shell(tmp_path):
    # `pid_matched` is not a liveness check on the state file's pid — a
    # recycled pid is alive and belongs to something unrelated. It asks
    # whether that pid is one of THIS shell's live claude jobs.
    store = JournalStore(tmp_path)
    store.write(_entry())
    controller = FakeController(groups=())   # no live claude under this shell
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         controller=controller)

    assert controller.asked == [_PID]        # the journaled SHELL pid
    assert recorder.calls == []              # -> unknown, not a kick


# --------------------------------------------------------------------------
# The attempt counter's only reset is a CONFIRMED reachable observation.
# --------------------------------------------------------------------------

def test_a_reachable_observation_resets_the_attempt_counter(tmp_path):
    # The confirmed signal that a prior kick actually worked. Per
    # `bridge_kicks`'s docstring this is the only thing that clears the cap
    # — never a timer.
    store = JournalStore(tmp_path)
    store.write(_entry())
    kick_store = bridge_kicks.KickHistoryStore(tmp_path)
    kick_store.record_kick(_SID, 1_000.0)
    kick_store.record_kick(_SID, 2_000.0)
    assert kick_store.attempts(_SID) == 2

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, _Recorder(),
         state=_state("session_013C"), kick_store=kick_store)

    assert kick_store.attempts(_SID) == 0


def test_an_unknown_reachability_does_not_reset_the_attempt_counter(tmp_path):
    # Only a confirmed "reachable" is evidence that a reconnect worked. An
    # unknown is the absence of evidence, so it must leave the cap's memory
    # intact — otherwise a session whose state file goes missing would
    # silently earn itself a fresh set of kick attempts.
    store = JournalStore(tmp_path)
    store.write(_entry())
    kick_store = bridge_kicks.KickHistoryStore(tmp_path)
    kick_store.record_kick(_SID, 1_000.0)
    kick_store.record_kick(_SID, 2_000.0)
    assert kick_store.attempts(_SID) == 2

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, _Recorder(),
         state={}, kick_store=kick_store)

    assert kick_store.attempts(_SID) == 2


# --------------------------------------------------------------------------
# Lineage (#35): the observation that justified THIS kick.
# --------------------------------------------------------------------------

def test_the_kick_records_the_reachability_that_justified_it(tmp_path):
    # Without this the record silently degrades to a bare timestamp, and a
    # kick you cannot regenerate from its inputs is a claim you cannot
    # audit. The old detector recorded bridge_since/bridge_seen/stale_after;
    # those inputs no longer exist, so the record carries the new ones.
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         state=_state(None, status="waiting", waiting_for="permission prompt"),
         signal=_signal(tail_kind="mid-turn"))
    assert recorder.calls == [_PID]

    log = bridge_kicks.KickHistoryStore(tmp_path).attempt_log(_SID)
    assert len(log) == 1
    obs = log[0]
    assert obs["reachability"] == "unreachable"
    assert obs["bridge_session_id"] is None
    assert obs["field_present"] is True
    assert obs["pid_matched"] is True
    assert obs["status"] == "waiting"
    assert obs["waiting_for"] == "permission prompt"
    assert obs["pid"] == _PID                 # signalled (the journaled shell)
    assert obs["state_pid"] == _CLAUDE_PID    # the claude the state file described
    assert obs["cooldown_seconds"] == cfg.Config().get("bridge_kick_cooldown_seconds")
    assert obs["max_attempts"] == cfg.Config().get("bridge_kick_max_attempts")
    assert obs["config_defaults_version"] == cfg.CONFIG_DEFAULTS_VERSION


# --------------------------------------------------------------------------
# Guards carried over unchanged from Slice 2.
# --------------------------------------------------------------------------

def test_crashed_session_is_untouched_by_this_path(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(boot="a-boot-that-does-not-match"))  # boot mismatch -> crashed
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder)

    assert recorder.calls == []


def test_ghost_session_is_not_kicked(tmp_path):
    # Alive, same boot, but no controlling terminal -> GHOST, not LIVE.
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         probe=FakeProbe(tty=False))

    assert recorder.calls == []


def test_global_switch_off_kicks_nothing_even_with_a_true_per_session_override(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    settings_store.write_global_autokick(False)
    settings_store.write_session_autokick(_SID, True)  # ignored: global is a hard switch
    recorder = _Recorder()

    _run(store.scan().entries, settings_store, tmp_path, recorder)

    assert recorder.calls == []


def test_per_session_opt_out_is_not_kicked_while_global_is_on(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    settings_store.write_session_autokick(_SID, False)
    recorder = _Recorder()

    _run(store.scan().entries, settings_store, tmp_path, recorder)

    assert recorder.calls == []


def test_remote_control_watch_false_gates_the_whole_step(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         config=cfg.Config({"remote_control_watch": False}))

    assert recorder.calls == []


def test_skip_reason_is_printed_for_a_global_off_skip(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    settings_store.write_global_autokick(False)

    _run(store.scan().entries, settings_store, tmp_path, _Recorder())

    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "globally off" in out


def test_skip_reason_is_printed_for_a_per_session_opt_out(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    settings_store.write_session_autokick(_SID, False)

    _run(store.scan().entries, settings_store, tmp_path, _Recorder())

    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "opted out" in out


def test_claude_less_entry_is_skipped_without_error(tmp_path):
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=1, cwd="/home/u/p", host="tmux", shell="zsh",
        boot_id=_BOOT, now="2026-08-07T00:00:00Z", claude=None,
    ))
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder)

    assert recorder.calls == []


def test_duplicate_sids_are_kicked_at_most_once_per_sweep(tmp_path):
    # Two journal entries sharing a session id (a modeled state —
    # `duplicate_group` on the card) share the same state file, so they'd
    # share the same verdict at every guard: without a de-dup, both would
    # be kicked, sending two SIGTERMs and spawning two `claude --resume`
    # against one live conversation.
    store = JournalStore(tmp_path)
    store.write(_entry(pid=100))
    store.write(_entry(pid=200))
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder)

    assert len(recorder.calls) == 1


def test_a_corrupt_settings_file_kicks_nothing(tmp_path, capsys):
    """Fail CLOSED: an unreadable store reads as "no overrides", which would
    silently drop every per-session opt-out — and this step restarts LIVE
    processes. An absent file is fine; a corrupt one must stop the pass."""
    store = JournalStore(tmp_path)
    store.write(_entry())
    (tmp_path / settings.FILENAME).write_text("{not json", encoding="utf-8")
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder)

    assert recorder.calls == []
    assert "not auto-kicking anything" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Review fix-wave 2026-08-07, FIX 1 (CRITICAL) — the cooldown/cap guard
# against an indefinite restart loop when the reconnect keeps failing.
#
# THE bug: without this, the pass is stateless across sweeps, so calling
# `_kick_dropped_bridges` twice in a row against an UNCHANGED still-
# unreachable session kicks it TWICE — a failed reconnect would be
# re-kicked every 30s forever. Every test below calls the function MULTIPLE
# times against the SAME `tmp_path` (the state dir), so kick history
# persists between calls exactly as it would across `crr revive` sweeps 30s
# apart.
# --------------------------------------------------------------------------

def test_multi_pass_unchanged_unreachable_session_is_not_rekicked_within_cooldown(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()
    entries = store.scan().entries

    _run(entries, settings_store, tmp_path, recorder, now=10_000.0)
    assert recorder.calls == [_PID]

    capsys.readouterr()
    # Second pass, 30s later (the revive-timer cadence) — the state file
    # STILL reports a null bridgeSessionId (exactly like a reconnect that
    # failed), and the session is still LIVE at a clean boundary. Without
    # the cooldown this would kick again.
    _run(entries, settings_store, tmp_path, recorder, now=10_030.0)
    assert recorder.calls == [_PID]  # NOT re-kicked
    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "cooldown" in out.lower()


def test_past_the_cooldown_a_still_unreachable_session_is_kicked_again(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()
    entries = store.scan().entries

    _run(entries, settings_store, tmp_path, recorder, now=10_000.0)
    assert recorder.calls == [_PID]

    # Default cooldown is 600s — one second past it, eligible again.
    _run(entries, settings_store, tmp_path, recorder, now=10_601.0)
    assert recorder.calls == [_PID, _PID]


def test_cap_reached_stops_kicking_with_a_stated_reason(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()
    entries = store.scan().entries

    # Default cap is 3 consecutive attempts; space each pass past the
    # cooldown so only the CAP (not the cooldown) is under test here.
    for now in (10_000.0, 11_000.0, 12_000.0):
        _run(entries, settings_store, tmp_path, recorder, now=now)
    assert len(recorder.calls) == 3

    capsys.readouterr()
    _run(entries, settings_store, tmp_path, recorder, now=999_000.0)  # far past any cooldown
    assert len(recorder.calls) == 3  # the 4th attempt is refused by the CAP, not the cooldown
    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "cap" in out.lower()


def test_sid_returning_to_reachable_resets_the_counter_and_becomes_eligible_again(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()
    entries = store.scan().entries
    cap = cfg.Config().get("bridge_kick_max_attempts")

    for i in range(cap):
        _run(entries, settings_store, tmp_path, recorder, now=10_000.0 + i * 1000.0)
    assert len(recorder.calls) == cap

    # Capped now — one more pass while still unreachable changes nothing.
    _run(entries, settings_store, tmp_path, recorder, now=999_000.0)
    assert len(recorder.calls) == cap

    # The reconnect actually worked: this pass observes a live
    # bridgeSessionId — the confirmed signal that resets the counter, per
    # crr.core.bridge_kicks's docstring (never a timer).
    _run(entries, settings_store, tmp_path, recorder, now=999_100.0,
         state=_state("session_013C"))
    assert len(recorder.calls) == cap  # the reachable pass itself never kicks

    history = bridge_kicks.KickHistoryStore(tmp_path)
    assert history.attempts(_SID) == 0  # reset

    # Unreachable again later (a second, independent drop) — eligible again.
    _run(entries, settings_store, tmp_path, recorder, now=999_200.0)
    assert len(recorder.calls) == cap + 1


def test_kick_attempt_is_recorded_even_when_the_relaunch_fails(tmp_path):
    # The cap counts ATTEMPTS, not confirmed failures — there is no
    # same-sweep signal that a relaunch actually reconnected (see
    # crr.core.bridge_kicks.KickHistoryStore.record_kick's docstring).
    store = JournalStore(tmp_path)
    store.write(_entry())
    recorder = _Recorder(ok=False)

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         now=10_000.0)
    assert recorder.calls == [_PID]

    history = bridge_kicks.KickHistoryStore(tmp_path)
    assert history.attempts(_SID) == 1
    assert history.last_kick_ts(_SID) == 10_000.0


def test_kick_attempt_is_recorded_even_when_kick_itself_raises(tmp_path):
    # `ops.kick` shells out (signals, subprocess probes) and is not
    # guaranteed to always return an OpResult rather than raise. If the
    # attempt were only recorded AFTER a successful return, a raise here
    # would leave this sid with no recorded attempt — reopening the exact
    # restart-loop hole FIX 1 exists to close (next pass, 30s later, would
    # see no cooldown and no attempt count, and retry immediately).
    store = JournalStore(tmp_path)
    store.write(_entry())

    def boom(*a, **kw):
        raise OSError("signal delivery failed unexpectedly")

    with pytest.raises(OSError):
        _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, boom,
             now=10_000.0)

    history = bridge_kicks.KickHistoryStore(tmp_path)
    assert history.attempts(_SID) == 1
    assert history.last_kick_ts(_SID) == 10_000.0


def test_a_corrupt_kick_history_file_kicks_nothing(tmp_path, capsys):
    """Fail CLOSED, same reasoning as the settings-file guard: a corrupt
    kick-history file must not silently degrade to "no history", which
    would erase the cooldown/cap protection right when the file might be
    corrupt because of the very loop it exists to stop."""
    store = JournalStore(tmp_path)
    store.write(_entry())
    (tmp_path / bridge_kicks.FILENAME).write_text("{not json", encoding="utf-8")
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder)

    assert recorder.calls == []
    assert "kick history" in capsys.readouterr().err.lower()


def test_kick_store_can_be_injected_for_a_fake(tmp_path):
    # Mirrors settings_store's injection: a caller-supplied kick_store is
    # used as-is rather than one built from `sd` internally.
    store = JournalStore(tmp_path)
    store.write(_entry())
    fake_history_dir = tmp_path / "elsewhere"
    fake_history_dir.mkdir()
    injected = bridge_kicks.KickHistoryStore(fake_history_dir)
    recorder = _Recorder()

    _run(store.scan().entries, settings.SettingsStore(tmp_path), tmp_path, recorder,
         kick_store=injected)

    assert recorder.calls == [_PID]
    assert injected.attempts(_SID) == 1
    assert bridge_kicks.KickHistoryStore(tmp_path).attempts(_SID) == 0  # default path untouched
