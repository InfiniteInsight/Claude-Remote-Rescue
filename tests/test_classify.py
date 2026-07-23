import os
import subprocess

from crr import bootid, classify, journal

CURRENT_BOOT = "boot-current"


def make_entry(pid, boot_id=CURRENT_BOOT):
    return journal.new_entry(
        pid=pid, cwd="/w", shell="bash", host="tab", boot_id=boot_id
    )


def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_live(monkeypatch):
    monkeypatch.setattr(classify, "_ps_tty", lambda pid: "pts/3")
    entry = make_entry(os.getpid())
    assert classify.classify(entry, CURRENT_BOOT) == classify.LIVE


def test_ghost_no_controlling_tty(monkeypatch):
    entry = make_entry(os.getpid())
    for ps_out in ("?", "??", "", "-"):
        monkeypatch.setattr(classify, "_ps_tty", lambda pid, out=ps_out: out)
        assert classify.classify(entry, CURRENT_BOOT) == classify.GHOST


def test_crashed_dead_pid(monkeypatch):
    monkeypatch.setattr(classify, "_ps_tty", lambda pid: "pts/0")
    entry = make_entry(dead_pid())
    assert classify.classify(entry, CURRENT_BOOT) == classify.CRASHED


def test_crashed_boot_mismatch_even_if_pid_alive(monkeypatch):
    """[lesson: recycled pids] alive pid + old boot id must be crashed."""
    monkeypatch.setattr(classify, "_ps_tty", lambda pid: "pts/0")
    entry = make_entry(os.getpid(), boot_id="boot-from-a-previous-life")
    assert classify.classify(entry, CURRENT_BOOT) == classify.CRASHED


def test_unknown_boot_ids_never_match(monkeypatch):
    monkeypatch.setattr(classify, "_ps_tty", lambda pid: "pts/0")
    entry = make_entry(os.getpid(), boot_id="")
    assert classify.classify(entry, CURRENT_BOOT) == classify.CRASHED
    entry = make_entry(os.getpid())
    assert classify.classify(entry, "") == classify.CRASHED


def test_classify_uses_real_boot_id_by_default(monkeypatch):
    monkeypatch.setattr(classify, "_ps_tty", lambda pid: "pts/0")
    entry = make_entry(os.getpid(), boot_id=bootid.current_boot_id())
    assert classify.classify(entry) in (classify.LIVE,)


def test_pid_alive_own_pid():
    assert classify.pid_alive(os.getpid()) is True


def test_pid_alive_dead():
    assert classify.pid_alive(dead_pid()) is False


def test_pid_alive_eperm_means_alive(monkeypatch):
    def fake_kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(classify.os, "kill", fake_kill)
    assert classify.pid_alive(12345) is True


def test_ps_tty_real_invocation_does_not_crash():
    # Smoke: the portable ps invocation runs on this platform.
    out = classify._ps_tty(os.getpid())
    assert isinstance(out, str)
