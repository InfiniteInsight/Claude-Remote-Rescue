"""Adapter tests — the pure judgment logic, isolated from the OS.

The subprocess/OS edges (running `ps`, os.kill) are thin; the decisions
worth testing are the path resolution and the "does this tty string mean
a real terminal" rule, both extracted as pure helpers so they need no
platform gating.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from crr.adapters import process_probe, state_dir


# --- state_dir resolution (pure) -----------------------------------------

def test_state_dir_macos():
    home = Path("/Users/someone")
    got = state_dir.resolve("Darwin", env={}, home=home)
    assert got == home / "Library" / "Application Support" / "crr"


def test_state_dir_linux_respects_xdg():
    got = state_dir.resolve("Linux", env={"XDG_STATE_HOME": "/x/state"}, home=Path("/home/u"))
    assert got == Path("/x/state") / "crr"


def test_state_dir_linux_default_when_no_xdg():
    got = state_dir.resolve("Linux", env={}, home=Path("/home/u"))
    assert got == Path("/home/u") / ".local" / "state" / "crr"


# --- tty judgment (pure) --------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("pts/3", True),
    ("  pts/1  \n", True),
    ("ttys002", True),
    ("?", False),
    ("??", False),
    ("", False),
    ("   \n", False),
])
def test_tty_is_real(raw, expected):
    assert process_probe._tty_is_real(raw) is expected


# --- is_alive (real pids, POSIX) -----------------------------------------

def test_is_alive_true_for_current_process():
    probe = process_probe.PsProcessProbe(timeout_seconds=5)
    assert probe.is_alive(os.getpid()) is True


def test_is_alive_false_for_reaped_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child.terminate()
    child.wait()
    # Give the OS a beat to fully clear it.
    time.sleep(0.05)
    probe = process_probe.PsProcessProbe(timeout_seconds=5)
    assert probe.is_alive(child.pid) is False
