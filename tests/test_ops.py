"""Session-operation tests (core/ops) — the single classifier-gated home.

Both the CLI handlers and the web POST endpoint call these, so the
gate lives in one place (a gate that drifts between CLI and web is a
recycled-pid-kills-bystander bug waiting to happen). Driven by fakes, so
no platform gating.
"""

import pytest

from crr.core import contracts, ops
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
    def __init__(self, live=(), fail_spawn=False, fail_kill=False):
        # live=None means "liveness is unknown" (F16 tri-state) — distinct
        # from live=() (genuinely no live sessions).
        self._live = None if live is None else set(live)
        self.created = []
        self.killed = []
        self._fail_spawn = fail_spawn
        self._fail_kill = fail_kill

    def list_sessions(self):
        return None if self._live is None else set(self._live)

    def new_detached_session(self, name, cwd, argv):
        if self._live is None:
            raise AssertionError("must not spawn while tmux liveness is unknown")
        if self._fail_spawn:
            raise RuntimeError("tmux new-session boom")
        self.created.append((name, cwd, list(argv)))
        self._live.add(name)

    def kill_session(self, name):
        if self._live is None:
            raise AssertionError("must not kill while tmux liveness is unknown")
        if self._fail_kill:
            raise RuntimeError("tmux kill-session boom")
        self.killed.append(name)
        self._live.discard(name)


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
#
# Signature (Task 11): reopen(store, archive, tmux, controller, flags, boot,
# probe, pid, now, *, grace, tab_spawner=None) — CRASHED tests below pass an
# idle FakeController/FakeFlags (grace/kill accounting only matters on the
# GHOST branch, further down).

def _idle_ctrl_flags():
    return FakeController(groups=[]), FakeFlags()


def test_reopen_spawns_for_crashed_claude(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux = FakeTmux()
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW, grace=0.1, remote_control=True)
    assert res.ok
    name = f"crr-{_SID}"
    assert tmux.created and tmux.created[0][0] == name
    assert store.read(42)["tmux_session"] == name


def test_reopen_omits_remote_control_when_disabled(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux = FakeTmux()
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW, grace=0.1, remote_control=False)
    assert res.ok
    assert tmux.created[0][2] == ["claude", "--resume", _SID]


def test_reopen_refuses_claude_less(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, claude=None)
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, FakeTmux(), ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW, grace=0.1, remote_control=True)
    assert not res.ok


def test_reopen_live_refused(tmp_path):
    """LIVE has a running claude to act on — kick/close are the ops for
    that, not reopen (which would race a spawn against the live shell)."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="same-boot", claude=_claude())
    ctrl, flags = FakeController(groups=[200]), FakeFlags()
    res = ops.reopen(store, archive, FakeTmux(), ctrl, flags, FakeBoot("same-boot"),
                     FakeProbe(alive=True, tty=True), 42, _NOW, grace=0.1, remote_control=True)
    assert not res.ok
    assert "is live" in res.message
    assert ctrl.terminated == []
    assert flags.armed == {}
    with pytest.raises(KeyError):
        archive.read(_SID)
    assert store.read(42)  # untouched


def test_reopen_already_running_does_not_respawn(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux = FakeTmux(live={f"crr-{_SID}"})
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW, grace=0.1, remote_control=True)
    assert res.ok
    assert tmux.created == []  # already up


def test_reopen_crashed_path_unchanged(tmp_path):
    """CRASHED keeps its no-flag, no-archive shape: only GHOST touches
    flags/controller/archive."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux = FakeTmux()
    ctrl, flags = FakeController(groups=[200]), FakeFlags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW, grace=0.1, remote_control=True)
    assert res.ok
    assert ctrl.terminated == []
    assert flags.armed == {}
    assert archive.scan().records == []
    assert store.read(42)["tmux_session"] == f"crr-{_SID}"


# --- reopen + visible tab (macOS tab-spawn) -------------------------------

def test_reopen_opens_a_visible_tab_attaching_to_the_revived_session(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux, tab = FakeTmux(), FakeTabSpawner()
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW,
                     grace=0.1, tab_spawner=tab, remote_control=True)
    name = f"crr-{_SID}"
    assert res.ok
    assert tmux.created  # revived detached first (durable)
    # The tab attaches to the tmux session — word-form, no shell string.
    assert tab.opened == [(["tmux", "attach", "-t", name], None)]


def test_reopen_opens_a_tab_even_when_already_running(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    name = f"crr-{_SID}"
    tmux, tab = FakeTmux(live={name}), FakeTabSpawner()
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW,
                     grace=0.1, tab_spawner=tab, remote_control=True)
    assert res.ok
    assert tmux.created == []                 # not respawned
    assert tab.opened[0][0] == ["tmux", "attach", "-t", name]  # but a tab is opened


def test_reopen_tab_failure_does_not_fail_the_op(tmp_path):
    # Revival is primary and already durable; a tab failure is surfaced in
    # the message but must not turn a successful revival into a failure
    # (DESIGN's swallowed-exit-code lesson, inverted — no false failure).
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux, tab = FakeTmux(), FakeTabSpawner(fail=True)
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW,
                     grace=0.1, tab_spawner=tab, remote_control=True)
    assert res.ok
    assert "tab" in res.message.lower() and "fail" in res.message.lower()
    assert store.read(42)["tmux_session"] == f"crr-{_SID}"  # revival persisted


def test_reopen_unavailable_spawner_stays_detached(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux, tab = FakeTmux(), FakeTabSpawner(available=False)
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW,
                     grace=0.1, tab_spawner=tab, remote_control=True)
    assert res.ok
    assert tab.opened == []  # never consulted an unavailable spawner
    # [live bug, 2026-07-31] "did nothing" honesty: an unavailable spawner
    # must say why no tab appeared and how to attach manually.
    name = f"crr-{_SID}"
    assert f"tmux attach -t {name}" in res.message


def test_reopen_refuses_when_tmux_liveness_is_unknown(tmp_path):
    # F16: an unconfirmed tmux state must refuse honestly, never guess.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="entry-boot", claude=_claude())
    tmux = FakeTmux(live=None)
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot(), FakeProbe(), 42, _NOW, grace=0.1, remote_control=True)
    assert not res.ok
    assert "cannot determine" in res.message.lower()
    assert tmux.created == []
    assert store.read(42)  # untouched


# --- _open_tab (honesty when no spawner is available) ----------------------

def test_open_tab_no_spawner_gives_the_attach_command():
    msg, _landed = ops._open_tab(None, "crr-8a1b2c3d")
    assert msg == " (no tab spawner on this host — attach with: tmux attach -t crr-8a1b2c3d)"


def test_open_tab_unavailable_spawner_gives_the_attach_command():
    msg, _landed = ops._open_tab(FakeTabSpawner(available=False), "crr-8a1b2c3d")
    assert "tmux attach -t crr-8a1b2c3d" in msg


def test_open_tab_reports_whether_a_tab_actually_landed():
    # [user request, 2026-08-09] "the tab is not convenience — if I am clicking
    # reopen I want the tab." Callers cannot honour that while _open_tab only
    # returns prose; it must say whether one appeared.
    spawner = FakeTabSpawner()
    suffix, landed = ops._open_tab(spawner, "crr-8a1b2c3d")
    assert landed is True
    assert "opened in a new tab" in suffix

    suffix, landed = ops._open_tab(FakeTabSpawner(fail=True), "crr-8a1b2c3d")
    assert landed is False

    suffix, landed = ops._open_tab(None, "crr-8a1b2c3d")
    assert landed is False


def test_reopen_is_degraded_when_a_tab_was_expected_and_missed(tmp_path):
    # A revival with no tab, on a host that HAS tabs, is not a plain success.
    store, archive, tmux = JournalStore(tmp_path), ArchiveStore(tmp_path), FakeTmux()
    _seed(store, 42, boot="OLD", claude=_claude())
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot("NEW"), FakeProbe(alive=False),
                     42, _NOW, grace=0.1, remote_control=True,
                     tab_spawner=FakeTabSpawner(fail=True), tabs_expected=True)
    assert res.ok is True          # the session IS alive — never report otherwise
    assert res.degraded is True
    assert "tmux attach -t" in res.message


def test_reopen_is_not_degraded_where_the_host_has_no_tabs(tmp_path):
    # Headless Linux / SSH / a systemd timer can never open a tab; saying
    # "degraded" every time would make the signal meaningless.
    store, archive, tmux = JournalStore(tmp_path), ArchiveStore(tmp_path), FakeTmux()
    _seed(store, 42, boot="OLD", claude=_claude())
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot("NEW"), FakeProbe(alive=False),
                     42, _NOW, grace=0.1, remote_control=True,
                     tab_spawner=None, tabs_expected=False)
    assert res.ok is True
    assert res.degraded is False


def test_reopen_is_not_degraded_when_the_tab_opens(tmp_path):
    store, archive, tmux = JournalStore(tmp_path), ArchiveStore(tmp_path), FakeTmux()
    _seed(store, 42, boot="OLD", claude=_claude())
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot("NEW"), FakeProbe(alive=False),
                     42, _NOW, grace=0.1, remote_control=True,
                     tab_spawner=FakeTabSpawner(), tabs_expected=True)
    assert res.ok is True
    assert res.degraded is False


def test_ghost_reopen_with_no_tmux_session_at_all_is_degraded(tmp_path):
    # Getting NEITHER a tmux session nor a tab is worse than losing only the
    # tab, so it must not be the one outcome that still reads as plain green.
    # Not gated on tabs_expected: a missing tmux session matters on every
    # host, headless included.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    boot, probe = _ghost(store, 42)
    tmux = FakeTmux(fail_spawn=True)
    ctrl, flags = FakeController(groups=[]), FakeFlags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, boot, probe, 42, _NOW,
                     grace=0.1, remote_control=True, tab_spawner=None, tabs_expected=False)
    assert res.ok is True          # the conversation IS preserved in the archive
    assert res.degraded is True    # ...but nothing the user clicked for happened
    assert "watchdog will revive it" in res.message


def test_opresult_defaults_to_not_degraded():
    # Every other op constructs OpResult(ok, message) — none of them regress.
    assert ops.OpResult(True, "x").degraded is False


def test_open_tab_failed_spawn_still_gives_the_attach_command():
    # [live bug, 2026-08-09] On WSL with the WSLInterop binfmt handler
    # unregistered, wt.exe is on PATH but cannot exec: the spawn raises
    # OSError(errno 8) and reopen reported only "(tab spawn failed: [Errno 8]
    # Exec format error: 'wt.exe')". The revival was durable and the session
    # attachable the whole time, so this branch owes the same manual fallback
    # the unavailable-spawner branch gives — the errno alone reads as a dead end.
    msg, _landed = ops._open_tab(FakeTabSpawner(fail=True), "crr-8a1b2c3d")
    assert "osascript boom" in msg  # the cause is still reported, never swallowed
    assert "tmux attach -t crr-8a1b2c3d" in msg


# --- reopen (GHOST) — user request 2026-07-30 mobile rescue path ----------

def _ghost(store, pid):
    # same boot + alive + no controlling tty -> classify ghost
    _seed(store, pid, boot="B", claude=_claude())
    return FakeBoot("B"), FakeProbe(alive=True, tty=False)


def test_reopen_ghost_kills_flags_archives_and_spawns(tmp_path):
    """[user request 2026-07-30] a ghost's conversation must be rescuable
    from the dashboard: close-flag the orphan wrapper, kill claude's group,
    preserve to archive as ghost-restored, revive into detached tmux."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    boot, probe = _ghost(store, 42)
    ctrl, flags, tmux = FakeController(groups=[200]), FakeFlags(), FakeTmux()
    res = ops.reopen(store, archive, tmux, ctrl, flags, boot, probe, 42, _NOW, grace=0.1, remote_control=True)
    assert res.ok, res.message
    assert flags.read(42) == ("close", None)          # armed and retained
    assert ctrl.terminated == [(200, 0.1)]
    rec = archive.read(_SID)
    assert rec["reason"] == "ghost-restored"
    assert rec["entry"]["tmux_session"] == f"crr-{_SID}"
    with pytest.raises(KeyError):
        store.read(42)                                # delisted
    assert tmux.created == [(
        f"crr-{_SID}", "/p42",
        ["claude", "--resume", _SID, "--remote-control", "p42"],
    )]


def test_reopen_ghost_without_claude_group_spawns_without_flag(tmp_path):
    # groups=[] -> claude is already dead: no flag armed, still archived +
    # delisted + spawned (never arm a flag without a landing kill).
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    boot, probe = _ghost(store, 42)
    ctrl, flags, tmux = FakeController(groups=[]), FakeFlags(), FakeTmux()
    res = ops.reopen(store, archive, tmux, ctrl, flags, boot, probe, 42, _NOW, grace=0.1, remote_control=True)
    assert res.ok, res.message
    assert flags.read(42) is None
    assert archive.read(_SID)["reason"] == "ghost-restored"
    with pytest.raises(KeyError):
        store.read(42)
    assert tmux.created


def test_reopen_ghost_kill_failure_leaves_everything_untouched(tmp_path):
    # groups=[200], terminate raises OSError -> not ok, flag cleared,
    # entry still present, nothing archived, nothing spawned.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    boot, probe = _ghost(store, 42)
    ctrl = FakeController(groups=[200], raise_for={200: OSError("nope")})
    flags, tmux = FakeFlags(), FakeTmux()
    res = ops.reopen(store, archive, tmux, ctrl, flags, boot, probe, 42, _NOW, grace=0.1, remote_control=True)
    assert not res.ok
    assert flags.read(42) is None            # rolled back: no kill landed
    assert store.read(42)                    # entry untouched
    with pytest.raises(KeyError):
        archive.read(_SID)                   # nothing archived
    assert tmux.created == []                # nothing spawned


def test_reopen_ghost_spawn_failure_still_preserves(tmp_path):
    # tmux.new_detached_session raises -> res.ok is True (preservation
    # succeeded), message mentions the watchdog will revive; archive record
    # exists.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    boot, probe = _ghost(store, 42)
    ctrl, flags = FakeController(groups=[200]), FakeFlags()
    tmux = FakeTmux(fail_spawn=True)
    res = ops.reopen(store, archive, tmux, ctrl, flags, boot, probe, 42, _NOW, grace=0.1, remote_control=True)
    assert res.ok is True
    assert "watchdog" in res.message.lower()
    assert archive.read(_SID)["reason"] == "ghost-restored"
    with pytest.raises(KeyError):
        store.read(42)


def test_reopen_ghost_already_running_does_not_respawn(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    boot, probe = _ghost(store, 42)
    name = f"crr-{_SID}"
    ctrl, flags, tmux = FakeController(groups=[200]), FakeFlags(), FakeTmux(live={name})
    res = ops.reopen(store, archive, tmux, ctrl, flags, boot, probe, 42, _NOW, grace=0.1, remote_control=True)
    assert res.ok, res.message
    assert tmux.created == []  # already up, not respawned
    assert archive.read(_SID)["reason"] == "ghost-restored"


def test_reopen_ghost_refuses_when_tmux_liveness_is_unknown(tmp_path):
    # F16: the unknown-liveness refusal must happen BEFORE the ghost
    # branch's kill+archive steps, not after — those are irreversible
    # (a kill can't be undone), so the refusal has to land earlier.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    boot, probe = _ghost(store, 42)
    ctrl, flags, tmux = FakeController(groups=[200]), FakeFlags(), FakeTmux(live=None)
    res = ops.reopen(store, archive, tmux, ctrl, flags, boot, probe, 42, _NOW, grace=0.1, remote_control=True)
    assert not res.ok
    assert "cannot determine" in res.message.lower()
    assert ctrl.terminated == []          # never reached the kill step
    assert flags.armed == {}
    assert store.read(42)                 # entry untouched
    with pytest.raises(KeyError):
        archive.read(_SID)                # nothing archived


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
    # management entirely: archive (reason "untracked" — terminology change:
    # detmux -> untrack), then delist.
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
    assert records[0]["reason"] == "untracked"
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
        max_strikes=3, now=_NOW, remote_control_enabled=True,
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


def test_detmux_refuses_when_tmux_liveness_is_unknown(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    tab = FakeTabSpawner()
    res = ops.detmux(store, archive, FakeTmux(live=None), FakeBoot(), FakeProbe(), 42, _NOW,
                     tab_spawner=tab)
    assert not res.ok
    assert "cannot determine" in res.message.lower()
    assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"  # untouched
    assert tab.opened == []


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


# --- untmux ------------------------------------------------------------------
#
# Real un-tmux: kill the parked tmux session, relaunch `claude --resume <sid>`
# directly in a visible tab (no wrapper). Same classifier/parked/live/spawner
# gates as detmux, same order — spawner-availability refusal BEFORE the kill,
# so a missing spawner never destroys the tmux session.

def test_untmux_kills_spawns_archives_and_delists(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    tmux = FakeTmux(live={"crr-8a1b2c3d"})
    tab = FakeTabSpawner()
    res = ops.untmux(store, archive, tmux, FakeBoot(), FakeProbe(), 42, _NOW, tab_spawner=tab, remote_control=True)
    assert res.ok, res.message
    assert tmux.killed == ["crr-8a1b2c3d"]
    assert tab.opened == [(["claude", "--resume", _SID, "--remote-control", "p42"], f"/p42")]
    with pytest.raises(KeyError):
        store.read(42)
    records = archive.scan().records
    assert len(records) == 1
    assert records[0]["reason"] == "untmuxed"
    assert records[0]["entry"]["pid"] == 42
    assert "un-tmuxed 42" in res.message


def test_untmux_omits_remote_control_when_disabled(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    tmux = FakeTmux(live={"crr-8a1b2c3d"})
    tab = FakeTabSpawner()
    res = ops.untmux(store, archive, tmux, FakeBoot(), FakeProbe(), 42, _NOW, tab_spawner=tab, remote_control=False)
    assert res.ok, res.message
    assert tab.opened == [(["claude", "--resume", _SID], f"/p42")]
    with pytest.raises(KeyError):
        store.read(42)
    records = archive.scan().records
    assert len(records) == 1
    assert records[0]["reason"] == "untmuxed"
    assert records[0]["entry"]["pid"] == 42
    assert "un-tmuxed 42" in res.message
    assert "crr no longer manages it" in res.message


def test_untmux_refuses_missing_entry(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    res = ops.untmux(store, archive, FakeTmux(), FakeBoot(), FakeProbe(), 999, _NOW,
                      tab_spawner=FakeTabSpawner(), remote_control=True)
    assert not res.ok and "no session" in res.message


def test_untmux_refuses_live_session(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d", boot="same-boot")
    tmux = FakeTmux(live={"crr-8a1b2c3d"})
    res = ops.untmux(store, archive, tmux, FakeBoot("same-boot"),
                      FakeProbe(alive=True, tty=True), 42, _NOW, tab_spawner=FakeTabSpawner(), remote_control=True)
    assert not res.ok
    assert "not crashed" in res.message
    assert tmux.killed == []
    assert store.read(42)


def test_untmux_refuses_unparked_session(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, claude=_claude())
    res = ops.untmux(store, archive, FakeTmux(), FakeBoot(), FakeProbe(), 42, _NOW,
                      tab_spawner=FakeTabSpawner(), remote_control=True)
    assert not res.ok and "not tmux-parked" in res.message


def test_untmux_refuses_when_tmux_session_is_gone(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    res = ops.untmux(store, archive, FakeTmux(live=set()), FakeBoot(), FakeProbe(), 42, _NOW,
                      tab_spawner=FakeTabSpawner(), remote_control=True)
    assert not res.ok and "gone" in res.message
    assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"


def test_untmux_refuses_when_tmux_liveness_is_unknown(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    tmux = FakeTmux(live=None)
    res = ops.untmux(store, archive, tmux, FakeBoot(), FakeProbe(), 42, _NOW,
                      tab_spawner=FakeTabSpawner(), remote_control=True)
    assert not res.ok
    assert "cannot determine" in res.message.lower()
    assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"


def test_untmux_requires_a_tab_spawner_before_killing(tmp_path):
    # The gate order is load-bearing: a missing spawner must refuse BEFORE
    # any destructive step, so the tmux session survives the refusal.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    for tab in (None, FakeTabSpawner(available=False)):
        _seed_parked(store, 42, "crr-8a1b2c3d")
        tmux = FakeTmux(live={"crr-8a1b2c3d"})
        res = ops.untmux(store, archive, tmux, FakeBoot(), FakeProbe(), 42, _NOW, tab_spawner=tab, remote_control=True)
        assert not res.ok and "no terminal tab spawner" in res.message
        assert tmux.killed == []
        assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"


def test_untmux_kill_failure_leaves_entry_untouched(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    tmux = FakeTmux(live={"crr-8a1b2c3d"}, fail_kill=True)
    res = ops.untmux(store, archive, tmux, FakeBoot(), FakeProbe(), 42, _NOW,
                      tab_spawner=FakeTabSpawner(), remote_control=True)
    assert not res.ok
    assert "failed to kill" in res.message
    assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"
    assert archive.scan().records == []


def test_untmux_spawn_failure_after_kill_leaves_entry_for_the_watchdog(tmp_path):
    # Decided design: on spawn failure AFTER the kill, the journal entry is
    # left untouched (tmux_session field intact) so the next revive pass
    # re-parks the conversation in tmux — say so in the message.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed_parked(store, 42, "crr-8a1b2c3d")
    tmux = FakeTmux(live={"crr-8a1b2c3d"})
    tab = FakeTabSpawner(fail=True)
    res = ops.untmux(store, archive, tmux, FakeBoot(), FakeProbe(), 42, _NOW, tab_spawner=tab, remote_control=True)
    assert not res.ok
    assert tmux.killed == ["crr-8a1b2c3d"]
    assert "watchdog" in res.message
    assert store.read(42)["tmux_session"] == "crr-8a1b2c3d"
    assert archive.scan().records == []


# --- retrack ------------------------------------------------------------------
#
# The undo of untrack/detmux: read the archive record, re-journal its entry,
# remove the record. Only records archived for that reason are eligible.

def _archive_untracked(archive, pid=42, reason="untracked", now=_NOW):
    entry = new_entry(
        pid=pid, cwd=f"/p{pid}", host="tmux", shell="zsh",
        boot_id="entry-boot", now=now, claude=_claude(),
    )
    archive.archive(entry, reason, now)
    return entry


def test_retrack_rejournals_and_removes_the_archive_record(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive)
    res = ops.retrack(store, archive, _SID, _NOW)
    assert res.ok, res.message
    entry = store.read(42)
    assert entry["claude"]["session_id"] == _SID
    with pytest.raises(KeyError):
        archive.read(_SID)


def test_retrack_stamps_updated_with_now(tmp_path):
    # Re-journaling is itself a change to the entry — a stale `updated`
    # (still the pre-untrack timestamp) would misrepresent when the entry
    # last changed.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive, now=_NOW)
    later = "2026-07-25T00:00:00Z"
    ops.retrack(store, archive, _SID, later)
    assert store.read(42)["updated"] == later


def test_retrack_message_reports_the_sid8(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive)
    res = ops.retrack(store, archive, _SID, _NOW)
    assert res.message == f"retracked {_SID[:8]}"


def test_retrack_accepts_the_deprecated_detmuxed_reason(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive, reason="detmuxed")
    res = ops.retrack(store, archive, _SID, _NOW)
    assert res.ok, res.message
    assert store.read(42)["claude"]["session_id"] == _SID
    with pytest.raises(KeyError):
        archive.read(_SID)


def test_retrack_entry_becomes_a_valid_journal_entry(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive)
    ops.retrack(store, archive, _SID, _NOW)
    contracts.validate_journal_entry(store.read(42))  # raises if invalid


def test_retrack_refuses_a_missing_archive_record(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    res = ops.retrack(store, archive, _SID, _NOW)
    assert not res.ok
    assert "no archived session" in res.message
    with pytest.raises(KeyError):
        store.read(42)


def test_retrack_refuses_a_malformed_sid(tmp_path):
    # archive.read raises ContractError (not KeyError) for a non-UUID sid —
    # both must refuse the same honest way, never let a bad sid reach a
    # path/glob.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    res = ops.retrack(store, archive, "not-a-uuid", _NOW)
    assert not res.ok
    assert "no archived session" in res.message


@pytest.mark.parametrize("reason", [
    "dismissed", "gave-up", "ghost-restored", "untmuxed",
    "superseded-on-register", "superseded-on-launch",
])
def test_retrack_refuses_non_untracked_reasons(tmp_path, reason):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive, reason=reason)
    res = ops.retrack(store, archive, _SID, _NOW)
    assert not res.ok
    assert "not untracked" in res.message
    # untouched: the record stays archived, nothing is journaled.
    assert archive.read(_SID)["reason"] == reason
    with pytest.raises(KeyError):
        store.read(42)


_OTHER_SID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


def test_retrack_works_when_the_pid_slot_is_free(tmp_path):
    # The recycled-pid guard must not block the ordinary case: nothing
    # occupies pid 42 in the journal, so retrack proceeds exactly as before.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive)
    with pytest.raises(KeyError):
        store.read(42)
    res = ops.retrack(store, archive, _SID, _NOW)
    assert res.ok, res.message
    assert store.read(42)["claude"]["session_id"] == _SID
    with pytest.raises(KeyError):
        archive.read(_SID)


def test_retrack_refuses_when_pid_slot_now_belongs_to_a_different_live_session(tmp_path):
    # A recycled pid: the OS handed 42 to a different, currently-tracked
    # session before retrack ran. Writing the archived entry there would
    # clobber the live one AND then archive.remove would destroy the only
    # remaining record of the conversation being retracked.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive, pid=42)
    live_claude = {
        "session_id": _OTHER_SID, "sid_source": "injected", "started": _NOW,
    }
    _seed(store, 42, claude=live_claude)
    res = ops.retrack(store, archive, _SID, _NOW)
    assert not res.ok
    assert "42" in res.message
    assert "different session" in res.message
    # The live entry must survive untouched.
    assert store.read(42)["claude"]["session_id"] == _OTHER_SID
    # The archive record must survive — refusing must not destroy it.
    assert archive.read(_SID)["reason"] == "untracked"


def test_retrack_refuses_when_the_pid_slot_is_already_this_sid(tmp_path):
    # Already tracked under that pid (e.g. a duplicate retrack request) —
    # refuse without touching the archive record.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive, pid=42)
    _seed(store, 42, claude=_claude())
    res = ops.retrack(store, archive, _SID, _NOW)
    assert not res.ok
    assert "already tracked" in res.message
    assert archive.read(_SID)["reason"] == "untracked"


def test_retrack_refuses_when_the_pid_slot_is_unreadable(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _archive_untracked(archive, pid=42)
    tabs_dir = tmp_path / "tabs"
    tabs_dir.mkdir(parents=True, exist_ok=True)
    (tabs_dir / "42.json").write_text("not json", encoding="utf-8")
    res = ops.retrack(store, archive, _SID, _NOW)
    assert not res.ok
    assert "42" in res.message
    # Refusing must not destroy the archive record.
    assert archive.read(_SID)["reason"] == "untracked"
    # The unreadable file itself must be left untouched — refusing must
    # not have written the archived entry over it.
    assert (tabs_dir / "42.json").read_text(encoding="utf-8") == "not json"


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
    def read(self, pid):
        return self.armed.get(pid)


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
