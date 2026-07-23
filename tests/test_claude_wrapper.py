"""claude() wrapper contract tests: real shell + fake claude executable.

The fake claude (see shim_helpers.make_fake_claude) records its argv and
any CRR_* environment variables it can see, then exits quickly -- enough
to check sid injection and the env-leakage lesson without needing the
real `claude` binary.
"""

from __future__ import annotations

import json
import shutil
import time

import pytest

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


def _read_log(log_path):
    if not log_path.exists():
        return ""
    return log_path.read_text()


def _single_entry(state_dir):
    tabs = state_dir / "tabs"
    if not tabs.is_dir():
        return None
    files = list(tabs.glob("*.json"))
    if len(files) != 1:
        return None
    return json.loads(files[0].read_text())


def _setup(tmp_path, shell):
    state = tmp_path / "state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    crr_bin = make_dev_crr_bin(tmp_path)
    log_path = tmp_path / "claude.log"
    make_fake_claude(bin_dir, log_path, sleep_seconds=0.3)
    env = base_env(state, crr_bin, extra_path=bin_dir)
    shim_path = SHIMS_DIR / SHELLS[shell]
    return state, log_path, env, shim_path


def test_fresh_launch_injects_session_id(tmp_path, shell):
    state, log_path, env, shim_path = _setup(tmp_path, shell)
    session = ShellSession(_argv(shell), env)
    try:
        session.send("source %s" % shim_path, settle=0.2)
        assert wait_for(lambda: _single_entry(state), timeout=5) is not None
        session.send("claude foo bar", settle=0.8)

        log = wait_for(lambda: _read_log(log_path) or None, timeout=5) or ""
        assert "ARGV: foo bar --session-id " in log

        entry = _single_entry(state)
        assert entry is not None
        assert entry["claude"] is not None
        assert entry["claude"]["verified"] is True
        sid = entry["claude"]["session_id"]
        assert sid and ("--session-id %s" % sid) in log
    finally:
        session.close()


def test_explicit_resume_sid_is_verified(tmp_path, shell):
    state, log_path, env, shim_path = _setup(tmp_path, shell)
    session = ShellSession(_argv(shell), env)
    try:
        session.send("source %s" % shim_path, settle=0.2)
        assert wait_for(lambda: _single_entry(state), timeout=5) is not None
        known_sid = "11111111-2222-3333-4444-555555555555"
        session.send("claude --resume %s" % known_sid, settle=0.8)

        wait_for(lambda: _read_log(log_path) or None, timeout=5)
        entry = wait_for(
            lambda: (_single_entry(state) or {}).get("claude") and _single_entry(state),
            timeout=5,
        )
        assert entry is not None
        assert entry["claude"]["session_id"] == known_sid
        assert entry["claude"]["verified"] is True
        # No injection: --session-id is never added when --resume <sid> given.
        log = _read_log(log_path)
        assert "--session-id" not in log
        assert ("--resume %s" % known_sid) in log
    finally:
        session.close()


def test_bare_resume_guesses_unverified_sid(tmp_path, shell):
    state, log_path, env, shim_path = _setup(tmp_path, shell)

    # Seed a fake transcript dir so guess-sid has something to find.
    cwd = str(state.parent)  # arbitrary cwd used by the shell session
    projects_dir = tmp_path / "claude-projects"
    slug = cwd.replace("/", "-")
    proj = projects_dir / slug
    proj.mkdir(parents=True)
    transcript = proj / "existing-transcript-sid.jsonl"
    transcript.write_text("{}\n")
    env["CRR_CLAUDE_PROJECTS_DIR"] = str(projects_dir)

    session = ShellSession(_argv(shell), env)
    try:
        session.send("cd %s" % cwd, settle=0.2)
        session.send("source %s" % shim_path, settle=0.2)
        assert wait_for(lambda: _single_entry(state), timeout=5) is not None
        session.send("claude --resume", settle=0.5)

        # Wait for the fake claude to actually have run (proves the full
        # launch sequence -- including the guess-sid subprocess call --
        # completed) before inspecting the journaled verified flag. The
        # background re-verify sleeps ~10s by default and the fake claude
        # exits well within that, so verified must still read False here.
        log = wait_for(lambda: _read_log(log_path) or None, timeout=15)
        assert log, "fake claude never ran"
        entry = _single_entry(state)
        assert entry is not None
        assert entry["claude"] is not None
        assert entry["claude"]["verified"] is False
        assert entry["claude"]["session_id"] == "existing-transcript-sid"

        assert "ARGV: --resume" in log
        assert "--session-id" not in log
    finally:
        session.close()


def test_claude_env_scrubbed_of_crr_vars(tmp_path, shell):
    state, log_path, env, shim_path = _setup(tmp_path, shell)
    session = ShellSession(_argv(shell), env)
    try:
        session.send("source %s" % shim_path, settle=0.2)
        assert wait_for(lambda: _single_entry(state), timeout=5) is not None
        session.send("claude ping", settle=0.8)
        log = wait_for(lambda: _read_log(log_path) or None, timeout=5) or ""
        assert "CRR_STATE_DIR" not in log
        assert "CRR_BIN" not in log
    finally:
        session.close()
