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
