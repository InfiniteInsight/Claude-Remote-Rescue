"""Session-operation tests (core/ops) — the single classifier-gated home.

Both the CLI handlers and the web POST endpoint call these, so the
gate lives in one place (a gate that drifts between CLI and web is a
recycled-pid-kills-bystander bug waiting to happen). Driven by fakes, so
no platform gating.
"""

from crr.core import ops
from crr.core.archive import ArchiveStore
from crr.core.journal import JournalStore, new_entry

_NOW = "2026-07-24T00:00:00Z"
_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _claude():
    return {"session_id": _SID, "sid_source": "injected", "started": _NOW}


def _seed(store, pid, *, boot="entry-boot", claude=None):
    store.write(new_entry(
        pid=pid, cwd=f"/p{pid}", host="tmux", shell="zsh",
        boot_id=boot, now=_NOW, claude=claude,
    ))


class FakeBoot:
    def __init__(self, boot="current-boot"):
        self._boot = boot

    def current(self):
        return self._boot


class FakeProbe:
    def __init__(self, alive=True, tty=True):
        self._alive, self._tty = alive, tty

    def is_alive(self, pid):
        return self._alive

    def has_controlling_tty(self, pid):
        return self._tty


class FakeTmux:
    def __init__(self, live=()):
        self._live = set(live)
        self.created = []

    def list_sessions(self):
        return set(self._live)

    def new_detached_session(self, name, cwd, argv):
        self.created.append((name, cwd, list(argv)))
        self._live.add(name)


class FakeTabSpawner:
    def __init__(self, available=True, fail=False):
        self._available, self._fail = available, fail
        self.opened = []

    def available(self):
        return self._available

    def open_tab(self, argv, cwd=None):
        if self._fail:
            raise RuntimeError("osascript boom")
        self.opened.append((list(argv), cwd))


# --- remove ---------------------------------------------------------------

def test_remove_deletes_and_is_idempotent(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=_claude())
    assert ops.remove(store, 42).ok
    assert not store.tabs_dir.joinpath("42.json").exists()
    assert ops.remove(store, 42).ok  # idempotent


# --- dismiss --------------------------------------------------------------

def test_dismiss_archives_and_delists_crashed(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())  # boot mismatch => crashed
    res = ops.dismiss(store, archive, FakeBoot(), FakeProbe(), 42, _NOW)
    assert res.ok
    assert not store.tabs_dir.joinpath("42.json").exists()
    assert archive.read(_SID)["reason"] == "dismissed"


def test_dismiss_refuses_live(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="same-boot", claude=_claude())
    res = ops.dismiss(store, archive, FakeBoot("same-boot"), FakeProbe(alive=True, tty=True), 42, _NOW)
    assert not res.ok
    assert store.tabs_dir.joinpath("42.json").exists()  # untouched


def test_dismiss_missing_session(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    assert not ops.dismiss(store, archive, FakeBoot(), FakeProbe(), 999, _NOW).ok


# --- reopen ---------------------------------------------------------------

def test_reopen_spawns_for_crashed_claude(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux = FakeTmux()
    res = ops.reopen(store, tmux, FakeBoot(), FakeProbe(), 42, _NOW)
    assert res.ok
    name = f"crr-{_SID[:8]}"
    assert tmux.created and tmux.created[0][0] == name
    assert store.read(42)["tmux_session"] == name


def test_reopen_refuses_claude_less(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=None)
    assert not ops.reopen(store, FakeTmux(), FakeBoot(), FakeProbe(), 42, _NOW).ok


def test_reopen_refuses_live(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, boot="same-boot", claude=_claude())
    res = ops.reopen(store, FakeTmux(), FakeBoot("same-boot"), FakeProbe(alive=True, tty=True), 42, _NOW)
    assert not res.ok


def test_reopen_already_running_does_not_respawn(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux = FakeTmux(live={f"crr-{_SID[:8]}"})
    res = ops.reopen(store, tmux, FakeBoot(), FakeProbe(), 42, _NOW)
    assert res.ok
    assert tmux.created == []  # already up


# --- reopen + visible tab (macOS tab-spawn) -------------------------------

def test_reopen_opens_a_visible_tab_attaching_to_the_revived_session(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux, tab = FakeTmux(), FakeTabSpawner()
    res = ops.reopen(store, tmux, FakeBoot(), FakeProbe(), 42, _NOW, tab_spawner=tab)
    name = f"crr-{_SID[:8]}"
    assert res.ok
    assert tmux.created  # revived detached first (durable)
    # The tab attaches to the tmux session — word-form, no shell string.
    assert tab.opened == [(["tmux", "attach", "-t", name], None)]


def test_reopen_opens_a_tab_even_when_already_running(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    name = f"crr-{_SID[:8]}"
    tmux, tab = FakeTmux(live={name}), FakeTabSpawner()
    res = ops.reopen(store, tmux, FakeBoot(), FakeProbe(), 42, _NOW, tab_spawner=tab)
    assert res.ok
    assert tmux.created == []                 # not respawned
    assert tab.opened[0][0] == ["tmux", "attach", "-t", name]  # but a tab is opened


def test_reopen_tab_failure_does_not_fail_the_op(tmp_path):
    # Revival is primary and already durable; a tab failure is surfaced in
    # the message but must not turn a successful revival into a failure
    # (DESIGN's swallowed-exit-code lesson, inverted — no false failure).
    store = JournalStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux, tab = FakeTmux(), FakeTabSpawner(fail=True)
    res = ops.reopen(store, tmux, FakeBoot(), FakeProbe(), 42, _NOW, tab_spawner=tab)
    assert res.ok
    assert "tab" in res.message.lower() and "fail" in res.message.lower()
    assert store.read(42)["tmux_session"] == f"crr-{_SID[:8]}"  # revival persisted


def test_reopen_unavailable_spawner_stays_detached(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux, tab = FakeTmux(), FakeTabSpawner(available=False)
    res = ops.reopen(store, tmux, FakeBoot(), FakeProbe(), 42, _NOW, tab_spawner=tab)
    assert res.ok
    assert tab.opened == []  # never consulted an unavailable spawner


# --- kick / close -----------------------------------------------------------

class FakeController:
    def __init__(self, groups):
        self.groups = groups
        self.terminated = []          # (pgid, grace) per call
        self.raise_on_terminate = False

    def claude_groups(self, shell_pid):
        return list(self.groups)

    def terminate_group(self, pgid, grace_seconds):
        if self.raise_on_terminate:
            raise OSError("no such process group")
        self.terminated.append((pgid, grace_seconds))


class FakeFlags:
    def __init__(self):
        self.armed = {}               # pid -> sid
    def arm(self, pid, sid):
        self.armed[pid] = sid
    def clear(self, pid):
        self.armed.pop(pid, None)


def _live(store, pid):
    # same boot + alive + tty  -> classify live
    _seed(store, pid, boot="B", claude=_claude())
    return FakeBoot("B"), FakeProbe(alive=True, tty=True)


def _crashed(store, pid):
    # boot mismatch -> classify crashed (regardless of pid liveness)
    _seed(store, pid, boot="B", claude=_claude())
    return FakeBoot("other"), FakeProbe(alive=True, tty=True)


# --- close ---------------------------------------------------------------

def test_close_terminates_the_claude_group_of_a_live_session(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl = FakeController(groups=[555])
    res = ops.close(store, ctrl, boot, probe, 10, grace=5)
    assert res.ok is True
    assert ctrl.terminated == [(555, 5)]


def test_close_refuses_a_crashed_session(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _crashed(store, 10)
    ctrl = FakeController(groups=[555])
    res = ops.close(store, ctrl, boot, probe, 10, grace=5)
    assert res.ok is False
    assert ctrl.terminated == []          # never signalled


def test_close_reports_when_no_running_claude_group(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl = FakeController(groups=[])
    res = ops.close(store, ctrl, boot, probe, 10, grace=5)
    assert res.ok is False


# --- kick ----------------------------------------------------------------

def test_kick_arms_the_flag_then_terminates(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.kick(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is True
    assert flags.armed[10] == _SID        # sid armed
    assert ctrl.terminated == [(555, 5)]


def test_kick_rolls_the_flag_back_when_the_signal_fails(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    ctrl.raise_on_terminate = True
    res = ops.kick(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert 10 not in flags.armed          # flag survives only if the kill landed


def test_kick_refuses_a_crashed_session(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _crashed(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.kick(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert flags.armed == {}
    assert ctrl.terminated == []


def test_kick_refuses_a_claude_less_shell(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 11, boot="B", claude=None)   # registered shell, no claude
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.kick(store, ctrl, flags, FakeBoot("B"), FakeProbe(), 11, grace=5)
    assert res.ok is False
    assert flags.armed == {}
