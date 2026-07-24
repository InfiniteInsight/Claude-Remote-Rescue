"""Adapter tests — the pure judgment logic, isolated from the OS.

The subprocess/OS edges (running `ps`, os.kill) are thin; the decisions
worth testing are the path resolution and the "does this tty string mean
a real terminal" rule, both extracted as pure helpers so they need no
platform gating.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from crr.adapters import process_probe, state_dir, tmux


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


# --- tmux command builders (pure) ----------------------------------------

def test_new_session_cmd_is_word_form_after_dashdash():
    cmd = tmux._new_session_cmd("crr-abc", "/home/u/p", ["claude", "--resume", "sid-1"])
    assert cmd == [
        "tmux", "new-session", "-d", "-s", "crr-abc", "-c", "/home/u/p",
        "--", "claude", "--resume", "sid-1",
    ]


def test_parse_sessions_drops_blank_lines():
    assert tmux._parse_sessions("a\nb\n\n") == {"a", "b"}
    assert tmux._parse_sessions("") == set()


# --- real tmux integration (gated on tmux installed) ---------------------

@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_realtmux_creates_word_form_detached_session(tmp_path, monkeypatch):
    # Isolate from the user's tmux by pointing the server at a scratch dir.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    t = tmux.RealTmux(timeout_seconds=10)
    try:
        assert t.list_sessions() == set()  # fresh server: no sessions
        t.new_detached_session("crr-itest", str(tmp_path), ["sleep", "300"])
        assert "crr-itest" in t.list_sessions()

        # Word-form proof: the pane runs the target directly, not a shell.
        got = subprocess.run(
            ["tmux", "display", "-t", "crr-itest", "-p", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=10,
        )
        assert got.stdout.strip() == "sleep"
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
