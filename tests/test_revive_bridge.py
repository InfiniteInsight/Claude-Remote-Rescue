"""The dropped-Remote-Control watchdog step (spec 2026-08-07, Slice 2).

`cli._kick_dropped_bridges` is the separate, LIVE-acting pass appended to
`crr revive` after the existing crashed-session revival. Driven entirely by
fakes: a fake boot/probe (mirrors test_reviver.py's shape) decide the
classifier state, and `read_tail_facts`/`read_takeover_signal`/`kick` are
injected so no real transcript or process is ever touched. Every test
asserts on the fake `kick` being called (or not) — never on a real process.
"""

from crr import cli
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
