"""Shell shim contract tests — real shell subprocesses, isolated state.

A shim is shell code; only a real shell can prove it journals correctly.
These run the generated shim under an actual ``bash`` in a throwaway
``XDG_STATE_HOME`` and assert the shell registers at start (a valid,
claude-less entry appears) and deregisters at exit (the entry is gone).

Gated to Linux + bash present: the shim calls ``crr register``, whose
boot-identity adapter is Linux-only in Phase 1.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from crr import cli
from crr.core import contracts

_CRR_BIN = str(Path(sys.executable).parent / "crr")

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux" or shutil.which("bash") is None or not Path(_CRR_BIN).exists(),
    reason="needs Linux + bash + an installed crr console script",
)


def _write_bash_shim(tmp_path, capsys) -> Path:
    assert cli.main(["shim", "bash", "--crr-bin", _CRR_BIN]) == 0
    text = capsys.readouterr().out
    assert "@CRR_BIN@" not in text  # placeholder was substituted
    shim = tmp_path / "crr.bash"
    shim.write_text(text, encoding="utf-8")
    return shim


def _run_bash(script: str, state_dir: Path) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state_dir),
        "XDG_STATE_HOME": str(state_dir),
    }
    return subprocess.run(
        ["bash", "--norc", "--noprofile", "-c", script],
        env=env, capture_output=True, text=True, timeout=30,
    )


def test_bash_shim_registers_a_valid_entry(tmp_path, capsys):
    shim = _write_bash_shim(tmp_path, capsys)
    state = tmp_path / "state"
    # Source the shim, then print our own journal entry while still alive.
    script = f'source "{shim}"\ncat "$XDG_STATE_HOME/crr/tabs/$$.json"\n'
    result = _run_bash(script, state)
    assert result.returncode == 0, result.stderr

    entry = json.loads(result.stdout)
    contracts.validate_journal_entry(entry)
    assert entry["shell"] == "bash"
    assert entry["claude"] is None
    assert entry["host"] == "tab"  # no TMUX/SSH in the test env


def test_bash_shim_deregisters_on_exit(tmp_path, capsys):
    shim = _write_bash_shim(tmp_path, capsys)
    state = tmp_path / "state"
    # Record the pid, let the shell exit (firing the EXIT trap).
    script = f'source "{shim}"\necho "$$" > "{tmp_path}/pid"\n'
    result = _run_bash(script, state)
    assert result.returncode == 0, result.stderr

    pid = (tmp_path / "pid").read_text().strip()
    assert not (state / "crr" / "tabs" / f"{pid}.json").exists()


def test_bash_shim_records_last_cmd(tmp_path, capsys):
    shim = _write_bash_shim(tmp_path, capsys)
    state = tmp_path / "state"
    # Run a distinctive command, then read the entry: the DEBUG hook must
    # have moved last_cmd off its registered empty string.
    script = (
        f'source "{shim}"\n'
        'true marker-command\n'
        'cat "$XDG_STATE_HOME/crr/tabs/$$.json"\n'
    )
    result = _run_bash(script, state)
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)
    assert entry["last_cmd"] != ""
