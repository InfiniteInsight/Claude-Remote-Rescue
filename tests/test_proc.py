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


# --- kick must reach a tmux-parked claude (#58) ---------------------------
#
# claude_groups() finds claude CHILDREN of a journaled shell pid. A revived
# session is journaled under the claude process itself, which has no claude
# children — measured live: claude_groups(2016) == []. Without this case a
# re-keyed entry shows a Kick button that signals nothing.

def test_claude_groups_returns_the_pids_own_group_when_it_is_itself_claude():
    from crr.adapters.process_probe import _child_groups
    rows = [
        (1956, 1, 1956, "tmux"),      # the tmux server
        (2016, 1956, 2016, "claude"),  # parked claude: its own group leader
    ]
    assert _child_groups(rows, 2016) == [2016]


def test_claude_groups_still_finds_children_for_a_journaled_shell():
    from crr.adapters.process_probe import _child_groups
    rows = [
        (500, 1, 500, "fish"),
        (501, 500, 501, "claude"),
    ]
    assert _child_groups(rows, 500) == [501]


def test_claude_groups_does_not_return_a_non_claude_pids_own_group():
    from crr.adapters.process_probe import _child_groups
    rows = [(500, 1, 500, "fish")]
    assert _child_groups(rows, 500) == []
