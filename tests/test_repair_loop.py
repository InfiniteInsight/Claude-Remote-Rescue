"""End-to-end repair-loop test: a real shell, the claude() wrapper, and a
simulated `kick` (a relaunch flag written mid-session, exactly as
``ops.kick`` writes it once a signal has landed) -- verifies the wrapper
notices the flag after claude exits and re-execs `claude --resume <sid>`
with the same journaled sid, then stops (no infinite relaunch) once the
flag is gone.
"""

from __future__ import annotations

import json
import shutil

import pytest

from crr import journal

from shim_helpers import (
    SHIMS_DIR,
    ShellSession,
    base_env,
    make_dev_crr_bin,
    make_fake_claude,
    shell_available,
    wait_for,
)

SHELLS = {
    "bash": "crr.bash",
    "zsh": "crr.zsh",
    "fish": "crr.fish",
}


def _argv(shell: str):
    return [shutil.which(shell), "-i"]


@pytest.fixture(params=sorted(SHELLS))
def shell(request):
    name = request.param
    if not shell_available(name):
        pytest.skip("%s not installed in this container" % name)
    return name


def _single_entry(state_dir):
    tabs = state_dir / "tabs"
    if not tabs.is_dir():
        return None
    files = list(tabs.glob("*.json"))
    if len(files) != 1:
        return None
    return json.loads(files[0].read_text())


def test_kick_flag_triggers_resume_relaunch(tmp_path, shell, crr_state):
    """crr_state (conftest, autouse) has already pointed CRR_STATE_DIR at
    tmp_path/state for *this* pytest process -- journal.write_relaunch_flag
    below writes to the same directory the shim's crr subprocess reads."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    crr_bin = make_dev_crr_bin(tmp_path)
    log_path = tmp_path / "claude.log"
    # A longer sleep gives the test time to drop the relaunch flag while
    # the fake claude is still "running".
    make_fake_claude(bin_dir, log_path, sleep_seconds=0.6)
    env = base_env(state, crr_bin, extra_path=bin_dir)
    shim_path = SHIMS_DIR / SHELLS[shell]

    session = ShellSession(_argv(shell), env)
    try:
        session.send("source %s" % shim_path, settle=0.2)
        entry = wait_for(lambda: _single_entry(state), timeout=5)
        assert entry is not None
        pid = entry["pid"]

        # Launch claude in the background of the *test driver* (the pty
        # session itself stays foreground/blocking from the shell's point
        # of view) so we can drop the kick flag while it's still "running".
        session.send("claude first-launch &", settle=0.3)

        # Simulate `ops.kick` having landed: write the flag exactly the
        # way ops.py does, only once a signal actually lands.
        journal.write_relaunch_flag(pid)

        def two_invocations():
            if not log_path.exists():
                return False
            return log_path.read_text().count("---END---") >= 2

        assert wait_for(two_invocations, timeout=10), (
            "expected the wrapper to relaunch claude after the kick flag"
        )

        log = log_path.read_text()
        blocks = [b for b in log.split("---END---") if b.strip()]
        assert len(blocks) >= 2
        first, second = blocks[0], blocks[1]
        assert "ARGV: first-launch --session-id " in first
        # Pull the injected sid back out and confirm the relaunch resumes
        # that exact conversation.
        sid = first.split("--session-id ")[1].split()[0].strip()
        assert ("ARGV: --resume %s" % sid) in second

        # No third invocation: the flag was consumed, not re-triggered.
        assert wait_for(lambda: log_path.read_text().count("---END---") >= 3, timeout=2) is False
    finally:
        session.close()
