"""Shared adapter subprocess helper (crr.adapters._proc)."""

import os
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
