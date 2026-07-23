import os

from crr import classify, journal, ops
from crr.result import (
    EXIT_FAILED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_REFUSED,
)


def write_entry(pid, boot_id, **kw):
    entry = journal.new_entry(
        pid=pid, cwd="/w", shell="bash", host="tab", boot_id=boot_id, **kw
    )
    journal.write_entry(entry)
    return entry


class KillRecorder:
    """Replaces ops' signalling helpers; records instead of killing."""

    def __init__(self, monkeypatch):
        self.pids = []
        self.groups = []
        monkeypatch.setattr(ops, "_kill_pid", lambda pid, sig: self.pids.append((pid, sig)))
        monkeypatch.setattr(ops, "_kill_group", lambda pgid, sig: self.groups.append((pgid, sig)))

    @property
    def any(self):
        return bool(self.pids or self.groups)


def force_state(monkeypatch, state):
    monkeypatch.setattr(classify, "classify", lambda entry, boot=None: state)


# ---------------------------------------------------------------------------
# Gating: destructive ops refuse crashed entries (recycled-pid protection)


def test_kick_refuses_crashed_alive_pid(monkeypatch):
    """The core recycled-pid lesson: pid alive but boot mismatch ->
    classifier says crashed -> no signal is ever sent."""
    rec = KillRecorder(monkeypatch)
    write_entry(os.getpid(), boot_id="boot-from-before-the-reboot")
    res = ops.kick(os.getpid())
    assert res.ok is False
    assert res.status == "refused-crashed"
    assert res.state == classify.CRASHED
    assert res.exit_code == EXIT_REFUSED
    assert rec.any is False


def test_close_refuses_crashed(monkeypatch):
    rec = KillRecorder(monkeypatch)
    write_entry(os.getpid(), boot_id="stale-boot")
    res = ops.close(os.getpid())
    assert res.status == "refused-crashed"
    assert res.exit_code == EXIT_REFUSED
    assert rec.any is False


def test_dismiss_crashed_archives_without_signalling(monkeypatch):
    rec = KillRecorder(monkeypatch)
    write_entry(os.getpid(), boot_id="stale-boot")
    res = ops.dismiss(os.getpid())
    assert res.ok is True
    assert res.status == "dismissed"
    assert rec.any is False
    assert journal.read_entry(os.getpid()) is None
    assert len(journal.list_archived()) == 1


def test_ops_not_found():
    for fn in (ops.kick, ops.close, ops.reopen, ops.dismiss, ops.remove):
        res = fn(999999)
        assert res.ok is False
        assert res.status == "not-found"
        assert res.exit_code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# kick: kill-by-ancestry


def test_kick_live_kills_child_process_groups(monkeypatch):
    rec = KillRecorder(monkeypatch)
    force_state(monkeypatch, classify.LIVE)
    shell_pid = os.getpid()
    write_entry(shell_pid, boot_id="b")
    monkeypatch.setattr(ops, "_children_of", lambda pid, table=None: [11111, 22222])

    pgids = {11111: 11111, 22222: 22222, shell_pid: os.getpgid(shell_pid)}
    monkeypatch.setattr(ops.os, "getpgid", lambda pid: pgids[pid])

    res = ops.kick(shell_pid)
    assert res.ok is True
    assert res.status == "kicked"
    assert res.exit_code == EXIT_OK
    # Children own their groups -> group kills, no bare-pid kills.
    assert sorted(g for g, _ in rec.groups) == [11111, 22222]
    assert rec.pids == []
    assert res.extra["signalled"] == [11111, 22222]


def test_kick_child_sharing_shell_group_killed_individually(monkeypatch):
    rec = KillRecorder(monkeypatch)
    force_state(monkeypatch, classify.LIVE)
    shell_pid = os.getpid()
    shell_pgid = os.getpgid(shell_pid)
    write_entry(shell_pid, boot_id="b")
    monkeypatch.setattr(ops, "_children_of", lambda pid, table=None: [33333])
    monkeypatch.setattr(ops.os, "getpgid", lambda pid: shell_pgid)

    res = ops.kick(shell_pid)
    assert res.ok is True
    # The shell's own group must not be group-killed.
    assert rec.groups == []
    assert [p for p, _ in rec.pids] == [33333]


def test_kick_no_children_is_a_distinct_failure(monkeypatch):
    rec = KillRecorder(monkeypatch)
    force_state(monkeypatch, classify.LIVE)
    write_entry(os.getpid(), boot_id="b")
    monkeypatch.setattr(ops, "_children_of", lambda pid, table=None: [])
    res = ops.kick(os.getpid())
    assert res.ok is False
    assert res.status == "no-child"
    assert res.exit_code == EXIT_FAILED
    assert rec.any is False


# ---------------------------------------------------------------------------
# kick: relaunch flag [lesson: flag files]


def test_kick_writes_relaunch_flag_only_when_signalled(monkeypatch):
    rec = KillRecorder(monkeypatch)
    force_state(monkeypatch, classify.LIVE)
    shell_pid = os.getpid()
    write_entry(shell_pid, boot_id="b")
    monkeypatch.setattr(ops, "_children_of", lambda pid, table=None: [11111])
    monkeypatch.setattr(ops.os, "getpgid", lambda pid: 11111)

    assert journal.take_relaunch_flag(shell_pid) is False  # nothing yet

    res = ops.kick(shell_pid)
    assert res.ok is True
    # The flag is now present -- and take_relaunch_flag consumes it.
    assert journal.take_relaunch_flag(shell_pid) is True
    # Consuming is atomic: a second check finds nothing left.
    assert journal.take_relaunch_flag(shell_pid) is False


def test_kick_does_not_write_flag_when_no_child_to_signal(monkeypatch):
    rec = KillRecorder(monkeypatch)
    force_state(monkeypatch, classify.LIVE)
    shell_pid = os.getpid()
    write_entry(shell_pid, boot_id="b")
    monkeypatch.setattr(ops, "_children_of", lambda pid, table=None: [])

    res = ops.kick(shell_pid)
    assert res.ok is False
    assert journal.take_relaunch_flag(shell_pid) is False


def test_kick_refused_crashed_never_writes_flag(monkeypatch):
    rec = KillRecorder(monkeypatch)
    write_entry(os.getpid(), boot_id="boot-from-before-the-reboot")
    res = ops.kick(os.getpid())
    assert res.status == "refused-crashed"
    assert journal.take_relaunch_flag(os.getpid()) is False


# ---------------------------------------------------------------------------
# close / dismiss / reopen / remove semantics


def test_close_live_hups_shell(monkeypatch):
    rec = KillRecorder(monkeypatch)
    force_state(monkeypatch, classify.LIVE)
    write_entry(4444, boot_id="b")
    res = ops.close(4444)
    assert res.ok is True
    assert res.status == "closed"
    assert rec.pids == [(4444, ops.signal.SIGHUP)]


def test_dismiss_refuses_live(monkeypatch):
    rec = KillRecorder(monkeypatch)
    force_state(monkeypatch, classify.LIVE)
    write_entry(5555, boot_id="b")
    res = ops.dismiss(5555)
    assert res.ok is False
    assert res.status == "refused-live"
    assert res.exit_code == EXIT_REFUSED
    assert rec.any is False
    assert journal.read_entry(5555) is not None


def test_dismiss_ghost_hangs_up_and_archives(monkeypatch):
    rec = KillRecorder(monkeypatch)
    force_state(monkeypatch, classify.GHOST)
    write_entry(6666, boot_id="b")
    res = ops.dismiss(6666)
    assert res.ok is True
    assert rec.pids == [(6666, ops.signal.SIGHUP)]
    assert journal.read_entry(6666) is None
    assert len(journal.list_archived()) == 1


def test_reopen_refuses_non_crashed(monkeypatch):
    force_state(monkeypatch, classify.LIVE)
    write_entry(7777, boot_id="b")
    res = ops.reopen(7777)
    assert res.ok is False
    assert res.status == "refused-live"
    assert res.exit_code == EXIT_REFUSED


def test_remove_is_pure_delist(monkeypatch):
    rec = KillRecorder(monkeypatch)
    write_entry(8888, boot_id="whatever")
    res = ops.remove(8888)
    assert res.ok is True
    assert res.status == "removed"
    assert rec.any is False
    assert journal.read_entry(8888) is None
    assert journal.list_archived() == []


# ---------------------------------------------------------------------------
# status / gc


def test_status_attaches_state(monkeypatch):
    force_state(monkeypatch, classify.CRASHED)
    write_entry(1010, boot_id="old")
    items = ops.status()
    assert len(items) == 1
    assert items[0]["pid"] == 1010
    assert items[0]["state"] == classify.CRASHED


def test_gc_archives_crashed_without_sid(monkeypatch):
    force_state(monkeypatch, classify.CRASHED)
    write_entry(1, boot_id="old")  # no sid -> collected
    write_entry(2, boot_id="old", claude={"session_id": "abc", "started": "t"})
    stats = ops.gc()
    assert stats["archived"] == 1
    assert journal.read_entry(1) is None
    assert journal.read_entry(2) is not None


def test_process_table_and_children_real():
    """Smoke the real `ps -eo pid=,ppid=` path against this process."""
    table = ops._process_table()
    assert table  # non-empty on any POSIX box
    assert os.getpid() in table.get(os.getppid(), []) or any(
        os.getpid() in kids for kids in table.values()
    )
