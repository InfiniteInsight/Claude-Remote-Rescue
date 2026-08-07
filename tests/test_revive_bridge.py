"""The dropped-Remote-Control watchdog step (spec 2026-08-07, Slice 2).

`cli._kick_dropped_bridges` is the separate, LIVE-acting pass appended to
`crr revive` after the existing crashed-session revival. Driven entirely by
fakes: a fake boot/probe (mirrors test_reviver.py's shape) decide the
classifier state, and `read_tail_facts`/`read_takeover_signal`/`kick` are
injected so no real transcript or process is ever touched. Every test
asserts on the fake `kick` being called (or not) — never on a real process.
"""

import pytest

from crr import cli
from crr.core import bridge_kicks
from crr.core import config as cfg
from crr.core import settings
from crr.core.journal import JournalStore, new_entry
from crr.core.ops import OpResult

_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_BOOT = "current-boot-0000"
_PID = 4242


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


def _facts(bridge_seen=True, bridge_since=999):
    return {
        "last_prompt": "", "model": "", "last_active": "", "last_reply": "",
        "title": "", "slug": "", "transcript_bytes": 0,
        "bridge_seen": bridge_seen, "bridge_since": bridge_since,
    }


def _signal(tail_kind="assistant-end", mtime=0.0):
    return {"mtime": mtime, "tail_kind": tail_kind}


def test_live_dropped_ready_autokick_on_is_kicked(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == [_PID]
    assert "8a1b2c3d" in capsys.readouterr().out


def test_mid_turn_is_not_kicked_and_left_for_next_pass(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(tail_kind="mid-turn"),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []
    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "mid-turn" in out


def test_remote_control_ok_is_not_kicked(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(bridge_seen=True, bridge_since=0),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []


def test_remote_control_off_is_not_kicked(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(bridge_seen=False, bridge_since=0),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []


def test_crashed_session_is_untouched_by_this_path(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(boot="a-boot-that-does-not-match"))  # boot mismatch -> crashed
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []


def test_ghost_session_is_not_kicked(tmp_path):
    # Alive, same boot, but no controlling terminal -> GHOST, not LIVE.
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(tty=False), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []


def test_global_switch_off_kicks_nothing_even_with_a_true_per_session_override(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    settings_store.write_global_autokick(False)
    settings_store.write_session_autokick(_SID, True)  # ignored: global is a hard switch
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []


def test_per_session_opt_out_is_not_kicked_while_global_is_on(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    settings_store.write_session_autokick(_SID, False)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []


def test_remote_control_watch_false_gates_the_whole_step(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    config = cfg.Config({"remote_control_watch": False})
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), config, settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []


def test_skip_reason_is_printed_for_a_global_off_skip(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    settings_store.write_global_autokick(False)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "globally off" in out


def test_skip_reason_is_printed_for_a_per_session_opt_out(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    settings_store.write_session_autokick(_SID, False)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "opted out" in out


def test_claude_less_entry_is_skipped_without_error(tmp_path):
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=1, cwd="/home/u/p", host="tmux", shell="zsh",
        boot_id=_BOOT, now="2026-08-07T00:00:00Z", claude=None,
    ))
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []


def test_duplicate_sids_are_kicked_at_most_once_per_sweep(tmp_path):
    # Two journal entries sharing a session id (a modeled state —
    # `duplicate_group` on the card) share the same transcript, so they'd
    # share the same verdict at every guard: without a de-dup, both would
    # be kicked, sending two SIGTERMs and spawning two `claude --resume`
    # against one live conversation.
    store = JournalStore(tmp_path)
    store.write(_entry(pid=100))
    store.write(_entry(pid=200))
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert len(recorder.calls) == 1


def test_a_corrupt_settings_file_kicks_nothing(tmp_path, capsys):
    """Fail CLOSED: an unreadable store reads as "no overrides", which would
    silently drop every per-session opt-out — and this step restarts LIVE
    processes. An absent file is fine; a corrupt one must stop the pass."""
    store = JournalStore(tmp_path)
    store.write(_entry())
    (tmp_path / settings.FILENAME).write_text("{not json", encoding="utf-8")
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []
    assert "not auto-kicking anything" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Review fix-wave 2026-08-07, FIX 1 (CRITICAL) — the cooldown/cap guard
# against an indefinite restart loop when the reconnect keeps failing.
#
# THE bug: without this, the pass is stateless across sweeps, so calling
# `_kick_dropped_bridges` twice in a row against an UNCHANGED still-dropped
# session kicks it TWICE — a failed reconnect would be re-kicked every 30s
# forever. Every test below calls the function MULTIPLE times against the
# SAME `tmp_path` (the state dir), so kick history persists between calls
# exactly as it would across `crr revive` sweeps 30s apart.
# --------------------------------------------------------------------------

def _kick(entries, settings_store, tmp_path, recorder, *, now):
    return cli._kick_dropped_bridges(
        entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        JournalStore(tmp_path), tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: now,
    )


def test_multi_pass_unchanged_dropped_session_is_not_rekicked_within_cooldown(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()
    entries = store.scan().entries

    _kick(entries, settings_store, tmp_path, recorder, now=10_000.0)
    assert recorder.calls == [_PID]

    capsys.readouterr()
    # Second pass, 30s later (the revive-timer cadence) — bridge_since is
    # STILL past the threshold (the fake read_tail_facts always reports the
    # same "dropped" facts, exactly like a reconnect that failed), and the
    # session is still LIVE at a clean boundary. Without the cooldown this
    # would kick again.
    _kick(entries, settings_store, tmp_path, recorder, now=10_030.0)
    assert recorder.calls == [_PID]  # NOT re-kicked
    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "cooldown" in out.lower()


def test_past_the_cooldown_a_still_dropped_session_is_kicked_again(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()
    entries = store.scan().entries

    _kick(entries, settings_store, tmp_path, recorder, now=10_000.0)
    assert recorder.calls == [_PID]

    # Default cooldown is 600s — one second past it, eligible again.
    _kick(entries, settings_store, tmp_path, recorder, now=10_601.0)
    assert recorder.calls == [_PID, _PID]


def test_cap_reached_stops_kicking_with_a_stated_reason(tmp_path, capsys):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()
    entries = store.scan().entries

    # Default cap is 3 consecutive attempts; space each pass past the
    # cooldown so only the CAP (not the cooldown) is under test here.
    _kick(entries, settings_store, tmp_path, recorder, now=10_000.0)
    _kick(entries, settings_store, tmp_path, recorder, now=11_000.0)
    _kick(entries, settings_store, tmp_path, recorder, now=12_000.0)
    assert len(recorder.calls) == 3

    capsys.readouterr()
    _kick(entries, settings_store, tmp_path, recorder, now=999_000.0)  # far past any cooldown
    assert len(recorder.calls) == 3  # the 4th attempt is refused by the CAP, not the cooldown
    out = capsys.readouterr().out
    assert "8a1b2c3d" in out
    assert "cap" in out.lower()


def test_sid_returning_to_ok_resets_the_counter_and_becomes_eligible_again(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()
    entries = store.scan().entries

    for i in range(cfg.Config().get("bridge_kick_max_attempts")):
        cli._kick_dropped_bridges(
            entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
            store, tmp_path, controller=None, flags=None,
            read_tail_facts=lambda sid, cap, **kw: _facts(),
            read_takeover_signal=lambda sid: _signal(),
            kick=recorder, clock=lambda: 10_000.0 + i * 1000.0,
        )
    assert len(recorder.calls) == cfg.Config().get("bridge_kick_max_attempts")

    # Capped now — one more pass while still "dropped" changes nothing.
    cli._kick_dropped_bridges(
        entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 999_000.0,
    )
    assert len(recorder.calls) == cfg.Config().get("bridge_kick_max_attempts")

    # The reconnect actually worked: this pass observes remote_control="ok"
    # (bridge_since within the threshold) — the confirmed signal that resets
    # the counter, per crr.core.bridge_kicks's docstring (never a timer).
    cli._kick_dropped_bridges(
        entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(bridge_seen=True, bridge_since=0),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 999_100.0,
    )
    assert len(recorder.calls) == cfg.Config().get("bridge_kick_max_attempts")  # "ok" pass itself never kicks

    history = bridge_kicks.KickHistoryStore(tmp_path)
    assert history.attempts(_SID) == 0  # reset

    # Dropped again later (a second, independent drop) — eligible again.
    cli._kick_dropped_bridges(
        entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 999_200.0,
    )
    assert len(recorder.calls) == cfg.Config().get("bridge_kick_max_attempts") + 1


def test_kick_attempt_is_recorded_even_when_the_relaunch_fails(tmp_path):
    # The cap counts ATTEMPTS, not confirmed failures — there is no
    # same-sweep signal that a relaunch actually reconnected (see
    # crr.core.bridge_kicks.KickHistoryStore.record_kick's docstring).
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder(ok=False)
    entries = store.scan().entries

    _kick(entries, settings_store, tmp_path, recorder, now=10_000.0)
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
    settings_store = settings.SettingsStore(tmp_path)
    entries = store.scan().entries

    def boom(*a, **kw):
        raise OSError("signal delivery failed unexpectedly")

    with pytest.raises(OSError):
        cli._kick_dropped_bridges(
            entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
            store, tmp_path, controller=None, flags=None,
            read_tail_facts=lambda sid, cap, **kw: _facts(),
            read_takeover_signal=lambda sid: _signal(),
            kick=boom, clock=lambda: 10_000.0,
        )

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
    settings_store = settings.SettingsStore(tmp_path)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
    )

    assert recorder.calls == []
    assert "kick history" in capsys.readouterr().err.lower()


def test_kick_store_can_be_injected_for_a_fake(tmp_path):
    # Mirrors settings_store's injection: a caller-supplied kick_store is
    # used as-is rather than one built from `sd` internally.
    store = JournalStore(tmp_path)
    store.write(_entry())
    settings_store = settings.SettingsStore(tmp_path)
    fake_history_dir = tmp_path / "elsewhere"
    fake_history_dir.mkdir()
    injected = bridge_kicks.KickHistoryStore(fake_history_dir)
    recorder = _Recorder()

    cli._kick_dropped_bridges(
        store.scan().entries, FakeBoot(), FakeProbe(), cfg.Config(), settings_store,
        store, tmp_path, controller=None, flags=None,
        read_tail_facts=lambda sid, cap, **kw: _facts(),
        read_takeover_signal=lambda sid: _signal(),
        kick=recorder, clock=lambda: 10_000.0,
        kick_store=injected,
    )

    assert recorder.calls == [_PID]
    assert injected.attempts(_SID) == 1
    assert bridge_kicks.KickHistoryStore(tmp_path).attempts(_SID) == 0  # the default path untouched
