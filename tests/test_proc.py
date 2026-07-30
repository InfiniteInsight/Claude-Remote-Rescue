"""Shared adapter subprocess helper (crr.adapters._proc)."""

import os
import shutil
import signal
import subprocess

import pytest

from crr.adapters import _proc
from crr.adapters.process_probe import PsProcessController, _group_alive


def test_run_capture_returns_stdout_on_success():
    assert _proc.run_capture(["printf", "hello"], timeout=5) == "hello"


def test_run_capture_raises_on_nonzero_exit():
    # The "never swallow a nonzero exit" guard the diagnostics sources rely on.
    with pytest.raises(RuntimeError):
        _proc.run_capture(["sh", "-c", "echo boom >&2; exit 3"], timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_terminate_group_kills_a_real_process_group():
    # A sleeper in its OWN group (setsid), so terminating the group cannot
    # touch the test runner.
    proc = subprocess.Popen(["sleep", "60"], preexec_fn=os.setsid)
    pgid = os.getpgid(proc.pid)
    try:
        assert _group_alive(pgid) is True
        PsProcessController(2.0).terminate_group(pgid, grace_seconds=0.5)
        proc.wait(timeout=3)
        assert _group_alive(pgid) is False
    finally:
        if proc.poll() is None:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=3)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_claude_groups_selects_the_fake_claude_not_the_bg_job():
    # Real end-to-end proof of the `-o pid=,ppid=,pgid=,args=` parse and the
    # argv0-basename-prefix "claude" selection: a `claude-fake` child (the
    # DESIGN.md fake-tab pattern, `exec -a claude-fake sleep`) each in its
    # own process group, alongside a `make`-like bg job in a third group.
    # claude_groups(shell_pid=<this test's pid>) must return the fake
    # claude's group and never the bg job's — the exact bug this task fixes.
    claude_proc = subprocess.Popen(
        ["bash", "-c", "exec -a claude-fake sleep 60"], preexec_fn=os.setsid,
    )
    bg_proc = subprocess.Popen(["sleep", "60"], preexec_fn=os.setsid)
    try:
        claude_pgid = os.getpgid(claude_proc.pid)
        bg_pgid = os.getpgid(bg_proc.pid)
        groups = PsProcessController(2.0).claude_groups(os.getpid())
        assert claude_pgid in groups
        assert bg_pgid not in groups
    finally:
        for proc in (claude_proc, bg_proc):
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=3)
