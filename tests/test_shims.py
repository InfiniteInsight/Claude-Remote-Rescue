"""Shell-shim contract tests: real zsh/bash/fish subprocesses, sourcing
the shipped shim with CRR_STATE_DIR at a tmpdir and CRR_BIN at a real
(dev) crr entry point.

Skips per shell when that shell isn't installed in this container.
"""

from __future__ import annotations

import json
import shutil

import pytest

from shim_helpers import (
    SHIMS_DIR,
    ShellSession,
    base_env,
    make_dev_crr_bin,
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


def _skip_reason(shell: str) -> str:
    return "%s not installed in this container" % shell


@pytest.fixture(params=sorted(SHELLS))
def shell(request):
    name = request.param
    if not shell_available(name):
        pytest.skip(_skip_reason(name))
    return name


def _tabs_dir(state_dir):
    return state_dir / "tabs"


def _single_entry(state_dir):
    tabs = _tabs_dir(state_dir)
    if not tabs.is_dir():
        return None
    files = list(tabs.glob("*.json"))
    if len(files) != 1:
        return None
    return json.loads(files[0].read_text())


def test_register_on_start(tmp_path, shell):
    state = tmp_path / "state"
    state.mkdir()
    crr_bin = make_dev_crr_bin(tmp_path)
    env = base_env(state, crr_bin)
    shim_path = SHIMS_DIR / SHELLS[shell]

    session = ShellSession(_argv(shell), env)
    try:
        session.send("source %s" % shim_path, settle=0.2)
        entry = wait_for(lambda: _single_entry(state), timeout=5)
        assert entry is not None, "no journal entry appeared after sourcing"
        assert entry["shell"] == shell
        assert entry["host"] == "tab"
        assert entry["cwd"]
        assert entry["claude"] is None
    finally:
        session.close()


def test_last_cmd_updates_after_command(tmp_path, shell):
    state = tmp_path / "state"
    state.mkdir()
    crr_bin = make_dev_crr_bin(tmp_path)
    env = base_env(state, crr_bin)
    shim_path = SHIMS_DIR / SHELLS[shell]

    session = ShellSession(_argv(shell), env)
    try:
        session.send("source %s" % shim_path, settle=0.2)
        assert wait_for(lambda: _single_entry(state), timeout=5) is not None

        marker = "echo crr-marker-xyz"
        session.send(marker, settle=0.2)

        def last_cmd_matches():
            entry = _single_entry(state)
            return entry is not None and entry.get("last_cmd") == marker

        assert wait_for(last_cmd_matches, timeout=5)
    finally:
        session.close()


def test_entry_removed_on_clean_exit(tmp_path, shell):
    state = tmp_path / "state"
    state.mkdir()
    crr_bin = make_dev_crr_bin(tmp_path)
    env = base_env(state, crr_bin)
    shim_path = SHIMS_DIR / SHELLS[shell]

    session = ShellSession(_argv(shell), env)
    session.send("source %s" % shim_path, settle=0.2)
    assert wait_for(lambda: _single_entry(state), timeout=5) is not None
    session.close()

    tabs = _tabs_dir(state)
    files = list(tabs.glob("*.json")) if tabs.is_dir() else []
    assert files == [], "journal entry should be gone after clean exit"


def test_hooks_silent_noop_when_crr_bin_missing(tmp_path, shell):
    """[lesson: PATH poisoning] every hook must be a total, silent no-op
    when the CRR_BIN binary doesn't exist -- no output, no error text,
    and the shell keeps working normally."""
    state = tmp_path / "state"
    state.mkdir()
    missing_bin = tmp_path / "no-such-crr-binary"
    env = base_env(state, missing_bin)
    shim_path = SHIMS_DIR / SHELLS[shell]

    session = ShellSession(_argv(shell), env)
    session.send("source %s" % shim_path, settle=0.3)
    session.send("echo crr-still-works-123", settle=0.3)
    session.send("cd /tmp", settle=0.3)
    output = session.close()

    # No journal entry should ever have been created (crr is unusable).
    tabs = _tabs_dir(state)
    assert not tabs.is_dir() or list(tabs.glob("*.json")) == []

    # The shell must still function normally...
    assert "crr-still-works-123" in output
    # ...and nothing crr-related should have leaked into the transcript:
    # no python traceback, no "no such file" errors naming our (missing)
    # binary, no shell "command not found" for our own internals.
    lowered = output.lower()
    assert "traceback" not in lowered
    assert str(missing_bin) not in output
    for internal in ("_crr:", "_crr_preexec", "_crr_chpwd", "__crr_", "_crr_out:"):
        assert internal not in output
