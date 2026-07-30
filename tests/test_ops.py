"""Session-operation tests (core/ops) — the single classifier-gated home.

Both the CLI handlers and the web POST endpoint call these, so the
gate lives in one place (a gate that drifts between CLI and web is a
recycled-pid-kills-bystander bug waiting to happen). Driven by fakes, so
no platform gating.
"""

import pytest

from crr.core import ops
from crr.core.archive import ArchiveStore
from crr.core.journal import JournalStore, new_entry
from crr.core.reviver import revive_crashed

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


# --- detmux ----------------------------------------------------------------

def _seed_parked(store, pid, name, *, boot="entry-boot"):
    _seed(store, pid, boot=boot, claude=_claude())
    e = store.read(pid)
    e["tmux_session"] = name
    store.write(e)


def test_detmux_archives_and_delists_the_entry(tmp_path):
    # The reviver owns tmux_session (its reset branch re-parks a cleared
    # field within one watchdog pass and would later resurrect the
    # conversation) — successful detmux must take the entry out of crr's
    # management entirely: archive (reason "detmuxed"), then delist.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    tab = FakeTabSpawner()
    res = ops.detmux(store, archive, FakeTmux(live={"crr-8a1b2c3d"}), FakeBoot(), FakeProbe(),
                     42, _NOW, tab_spawner=tab)
    assert res.ok, res.message
    assert tab.opened == [(["tmux", "attach", "-t", "crr-8a1b2c3d"], None)]
    with pytest.raises(KeyError):
        store.read(42)
    records = archive.scan().records
    assert len(records) == 1
    assert records[0]["reason"] == "detmuxed"
    assert records[0]["entry"]["pid"] == 42


def test_detmux_delists_a_claude_less_parked_entry_without_archiving(tmp_path):
    # pid-reuse shape: an entry can carry a tmux_session with no claude
    # session attached. Mirrors dismiss's claude-less delist-without-archive.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, claude=None)
    e = store.read(42)
    e["tmux_session"] = "crr-8a1b2c3d"
    store.write(e)
    tab = FakeTabSpawner()
    res = ops.detmux(store, archive, FakeTmux(live={"crr-8a1b2c3d"}), FakeBoot(), FakeProbe(),
                     42, _NOW, tab_spawner=tab)
    assert res.ok, res.message
    with pytest.raises(KeyError):
        store.read(42)
    assert archive.scan().records == []


def test_detmux_leaves_the_reviver_nothing_to_repark(tmp_path):
    # Locks the finding's regression: after a successful detmux, the
    # reviver must find nothing to re-park or resurrect for this session.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    tab = FakeTabSpawner()
    res = ops.detmux(store, archive, FakeTmux(live={"crr-8a1b2c3d"}), FakeBoot(), FakeProbe(),
                     42, _NOW, tab_spawner=tab)
    assert res.ok, res.message

    tmux = FakeTmux(live={"crr-8a1b2c3d"})
    outcome = revive_crashed(
        store.scan().entries, FakeBoot(), FakeProbe(), tmux, store, archive,
        max_strikes=3, now=_NOW,
    )
    assert outcome.revived == []
    assert tmux.created == []


def test_detmux_refuses_missing_entry(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    res = ops.detmux(store, archive, FakeTmux(), FakeBoot(), FakeProbe(), 999, _NOW,
                     tab_spawner=FakeTabSpawner())
    assert not res.ok and "no session" in res.message


def test_detmux_refuses_live_session(tmp_path):
    """[bug 2026-07-29] DESIGN: ALL session ops are classifier-gated. A live
    shell that inherited tmux_session via same-boot pid preservation must not
    be archived+delisted out of crr management."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d", boot="same-boot")
    tab = FakeTabSpawner()
    res = ops.detmux(store, archive, FakeTmux(live={"crr-8a1b2c3d"}),
                     FakeBoot("same-boot"), FakeProbe(alive=True, tty=True),
                     42, _NOW, tab_spawner=tab)
    assert not res.ok
    assert "not crashed" in res.message
    assert store.read(42)                  # entry untouched
    assert tab.opened == []                # no tab opened


def test_detmux_refuses_unparked_session(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, claude=_claude())
    res = ops.detmux(store, archive, FakeTmux(), FakeBoot(), FakeProbe(), 42, _NOW,
                     tab_spawner=FakeTabSpawner())
    assert not res.ok and "not tmux-parked" in res.message


def test_detmux_refuses_when_tmux_session_is_gone(tmp_path):
    # Liveness comes from tmux, never the stored field (reviver lesson);
    # a stale field refuses and mutates nothing.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    res = ops.detmux(store, archive, FakeTmux(live=set()), FakeBoot(), FakeProbe(), 42, _NOW,
                     tab_spawner=FakeTabSpawner())
    assert not res.ok and "gone" in res.message
    assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"


def test_detmux_requires_a_tab_spawner(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    for tab in (None, FakeTabSpawner(available=False)):
        res = ops.detmux(store, archive, FakeTmux(live={"crr-8a1b2c3d"}), FakeBoot(), FakeProbe(),
                         42, _NOW, tab_spawner=tab)
        assert not res.ok and "no terminal tab spawner" in res.message
    assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"


def test_detmux_spawn_failure_keeps_bookkeeping(tmp_path):
    # The tab IS the operation: a spawn failure fails the op and the card
    # must keep offering the button.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    res = ops.detmux(store, archive, FakeTmux(live={"crr-8a1b2c3d"}), FakeBoot(), FakeProbe(),
                     42, _NOW, tab_spawner=FakeTabSpawner(fail=True))
    assert not res.ok and "failed to open a tab" in res.message
    assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"


# --- kick / close -----------------------------------------------------------

class FakeController:
    def __init__(self, groups, raise_for=None):
        self.groups = groups
        self.terminated = []          # (pgid, grace) per call
        self.raise_on_terminate = False   # legacy: raise OSError for every pgid
        self.raise_for = dict(raise_for or {})  # pgid -> exception, per-pgid failure

    def claude_groups(self, shell_pid):
        return list(self.groups)

    def terminate_group(self, pgid, grace_seconds):
        if self.raise_on_terminate:
            raise OSError("no such process group")
        if pgid in self.raise_for:
            raise self.raise_for[pgid]
        self.terminated.append((pgid, grace_seconds))


class FakeFlags:
    def __init__(self):
        self.armed = {}               # pid -> (kind, sid|None)
    def arm_relaunch(self, pid, sid):
        self.armed[pid] = ("relaunch", sid)
    def arm_close(self, pid):
        self.armed[pid] = ("close", None)
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

def test_close_terminates_and_arms_the_close_flag(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is True
    assert ctrl.terminated == [(555, 5)]
    assert flags.armed[10] == ("close", None)


def test_close_rolls_the_flag_back_when_the_signal_fails(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    ctrl.raise_on_terminate = True
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert 10 not in flags.armed          # flag survives only if the kill landed


def test_close_keeps_flag_when_any_group_kill_lands(tmp_path):
    """[bug 2026-07-29] one landed kill + one OSError used to clear the flag —
    the wrapper then showed the crash prompt instead of silently closing."""
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl = FakeController(groups=[200, 300], raise_for={300: OSError("gone")})
    flags = FakeFlags()
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=0.1)
    assert res.ok is True
    assert 10 in flags.armed              # flag survives: a kill landed
    assert ctrl.terminated == [(200, 0.1)]


def test_close_clears_flag_when_no_kill_lands(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl = FakeController(groups=[200], raise_for={200: OSError("nope")})
    flags = FakeFlags()
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=0.1)
    assert res.ok is False
    assert 10 not in flags.armed          # nothing landed: flag rolled back


def test_close_refuses_a_crashed_session(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _crashed(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert flags.armed == {}
    assert ctrl.terminated == []


def test_close_reports_when_no_running_claude_group(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[]), FakeFlags()
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert flags.armed == {}


# --- kick ----------------------------------------------------------------

def test_kick_arms_the_flag_then_terminates(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.kick(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is True
    assert flags.armed[10] == ("relaunch", _SID)  # sid armed
    assert ctrl.terminated == [(555, 5)]


def test_kick_rolls_the_flag_back_when_the_signal_fails(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    ctrl.raise_on_terminate = True
    res = ops.kick(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert 10 not in flags.armed          # flag survives only if the kill landed


def test_kick_keeps_flag_when_any_group_kill_lands(tmp_path):
    """[bug 2026-07-29] one landed kill + one OSError used to clear the flag —
    the wrapper then showed the crash prompt instead of silently resuming."""
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl = FakeController(groups=[200, 300], raise_for={300: OSError("gone")})
    flags = FakeFlags()
    res = ops.kick(store, ctrl, flags, boot, probe, 10, grace=0.1)
    assert res.ok is True
    assert flags.armed.get(10) == ("relaunch", _SID)   # flag survives: a kill landed
    assert ctrl.terminated == [(200, 0.1)]


def test_kick_clears_flag_when_no_kill_lands(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl = FakeController(groups=[200], raise_for={200: OSError("nope")})
    flags = FakeFlags()
    res = ops.kick(store, ctrl, flags, boot, probe, 10, grace=0.1)
    assert res.ok is False
    assert 10 not in flags.armed          # nothing landed: flag rolled back


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
