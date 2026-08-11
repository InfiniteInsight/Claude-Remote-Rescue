"""Shared adapter subprocess helper (crr.adapters._proc)."""

import os
import shutil
import signal
import subprocess
import time
from unittest import mock

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


# --- a zombie is not alive (#65) ------------------------------------------
#
# `_group_alive` asked `killpg(pgid, 0)`, which succeeds for a ZOMBIE — a
# process that has exited but not been reaped. So after a successful
# SIGTERM the group still read as alive for the whole grace window, and
# terminate_group escalated to SIGKILL every time. Linux tolerates SIGKILL
# to a zombie-only group; macOS returns EPERM, which `_signal_groups`
# records as a failure — so on macOS `kick`/`close` reported "failed to
# signal" for a kill that had in fact landed.

@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_a_zombie_only_group_is_not_alive():
    proc = subprocess.Popen(["sleep", "60"], preexec_fn=os.setsid)
    pgid = os.getpgid(proc.pid)
    try:
        assert _group_alive(pgid) is True
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(proc.pid)],
                                   capture_output=True, text=True).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.05)
        else:
            pytest.skip("could not produce a zombie on this platform")
        # Deliberately NOT reaped yet: this is the state the real ops hit,
        # where the shim shell has not returned from wait() yet.
        # Diagnostic in the message: if ps cannot report zombies on this
        # platform (macOS CI has been the hard case), the log must say so
        # rather than leaving a bare True != False.
        from crr.adapters.process_probe import _group_states_cmd
        probe = subprocess.run(_group_states_cmd(), capture_output=True, text=True)
        rows = [ln for ln in probe.stdout.splitlines()
                if ln.split(None, 1) and ln.split(None, 1)[0] == str(pgid)]
        assert _group_alive(pgid) is False, (
            f"a zombie counted as a live process; ps rc={probe.returncode} "
            f"stderr={probe.stderr.strip()!r} rows-for-pgid={rows!r}"
        )
    finally:
        if proc.poll() is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        proc.wait(timeout=3)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_terminate_group_does_not_escalate_when_the_group_only_zombies():
    # The escalation is what raises EPERM on macOS. With zombies excluded
    # there is nothing left to escalate against.
    proc = subprocess.Popen(["sleep", "60"], preexec_fn=os.setsid)
    pgid = os.getpgid(proc.pid)
    kills = []
    real_killpg = os.killpg

    def spy(pg, sig):
        if sig == signal.SIGKILL:
            kills.append(pg)
        return real_killpg(pg, sig)

    try:
        with mock.patch.object(os, "killpg", spy):
            PsProcessController(2.0).terminate_group(pgid, grace_seconds=1.0)
        assert kills == [], "escalated to SIGKILL against a group that was already gone"
    finally:
        if proc.poll() is None:
            try:
                real_killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        proc.wait(timeout=3)
