"""Shell shim contract tests — real shell subprocesses, isolated state.

A shim is shell code; only a real shell can prove it journals correctly.
Each installed shell runs its generated shim under a throwaway
``XDG_STATE_HOME`` and must register at start (a valid, claude-less entry
appears) and deregister at exit (the entry is gone). These two hooks fire
deterministically even non-interactively.

The preexec/last-cmd hook is verified for bash only: bash's DEBUG trap
fires in ``bash -c``, whereas zsh/fish ``preexec`` fire only for
interactive command lines, which would need a pty to exercise here. The
shims themselves wire the hook the same way for all three; that gap is a
test-harness limit, not a shim gap, and is called out rather than hidden.

Gated to Linux (the shim calls ``crr register``, whose boot-identity
adapter is Linux-only in Phase 1) and to shells that are installed.
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

# Per-shell: the argv to run a -c script with no user rc, and the token
# that expands to the shell's own pid.
_SHELLS = {
    "bash": {"argv": ["bash", "--norc", "--noprofile", "-c"], "pid": "$$"},
    "zsh": {"argv": ["zsh", "-f", "-c"], "pid": "$$"},
    "fish": {"argv": ["fish", "--no-config", "-c"], "pid": "$fish_pid"},
}

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux" or not Path(_CRR_BIN).exists(),
    reason="needs Linux + an installed crr console script",
)


def _installed(shell):
    return shutil.which(shell) is not None


def _make_shim(shell, tmp_path, capsys) -> Path:
    assert cli.main(["shim", shell, "--crr-bin", _CRR_BIN]) == 0
    text = capsys.readouterr().out
    assert "@CRR_BIN@" not in text
    shim = tmp_path / f"crr.{shell}"
    shim.write_text(text, encoding="utf-8")
    return shim


def _run(shell, script, state_dir) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state_dir),
        "XDG_STATE_HOME": str(state_dir),
    }
    return subprocess.run(
        _SHELLS[shell]["argv"] + [script],
        env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_shim_registers_a_valid_entry(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    pid = _SHELLS[shell]["pid"]
    script = f'source "{shim}"\ncat "$XDG_STATE_HOME/crr/tabs/{pid}.json"\n'
    result = _run(shell, script, state)
    assert result.returncode == 0, result.stderr

    entry = json.loads(result.stdout)
    contracts.validate_journal_entry(entry)
    assert entry["shell"] == shell
    assert entry["claude"] is None
    assert entry["host"] == "tab"  # no TMUX/SSH in the test env


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_shim_deregisters_on_exit(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    pid = _SHELLS[shell]["pid"]
    script = f'source "{shim}"\necho {pid} > "{tmp_path}/pid"\n'
    result = _run(shell, script, state)
    assert result.returncode == 0, result.stderr

    recorded = (tmp_path / "pid").read_text().strip()
    assert not (state / "crr" / "tabs" / f"{recorded}.json").exists()


def test_bash_shim_records_last_cmd(tmp_path, capsys):
    if not _installed("bash"):
        pytest.skip("bash not installed")
    shim = _make_shim("bash", tmp_path, capsys)
    state = tmp_path / "state"
    script = (
        f'source "{shim}"\n'
        "true marker-command\n"
        'cat "$XDG_STATE_HOME/crr/tabs/$$.json"\n'
    )
    result = _run("bash", script, state)
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)
    assert entry["last_cmd"] != ""
