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


def _fake_claude_bindir(tmp_path) -> Path:
    """A fake `claude` on PATH that records its argv and exits 0.

    The DESIGN fake-tab pattern: exercise the wrapper without a real
    claude. `command claude` in the wrapper resolves to this.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$CRR_TEST_RECORD\"\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return bindir


def _run_with_fake_claude(shell, script, state_dir, bindir, record, journal=None) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(state_dir),
        "XDG_STATE_HOME": str(state_dir),
        "CRR_TEST_RECORD": str(record),
    }
    if journal is not None:
        env["CRR_TEST_JOURNAL"] = str(journal)
    return subprocess.run(
        _SHELLS[shell]["argv"] + [script],
        env=env, capture_output=True, text=True, timeout=30,
    )


def _fake_claude_dumping_journal(tmp_path) -> Path:
    """A fake `claude` that records argv AND dumps the shell's live journal
    entry mid-run — captured BEFORE the wrapper's claude-exit clears the
    claude field, so a resume-journaled sid is observable.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$CRR_TEST_RECORD"\n'
        '[ -n "$CRR_TEST_JOURNAL" ] && '
        'cat "$XDG_STATE_HOME/crr/tabs/$PPID.json" > "$CRR_TEST_JOURNAL" 2>/dev/null\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return bindir


def _resume_journal(shell, tmp_path, capsys, cmdline):
    """Run `claude <cmdline>` under the shim and return the mid-run journal
    entry's claude field (None if the session was left untracked)."""
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_dumping_journal(tmp_path)
    record, journal = tmp_path / "argv", tmp_path / "journal"
    script = f'source "{shim}"\nclaude {cmdline}\n'
    result = _run_with_fake_claude(shell, script, state, bindir, record, journal=journal)
    assert result.returncode == 0, result.stderr
    if not journal.exists() or not journal.read_text().strip():
        return None
    return json.loads(journal.read_text())["claude"]


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_wrapper_injects_session_id_on_fresh_launch(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_bindir(tmp_path)
    record = tmp_path / "argv"
    script = f'source "{shim}"\nclaude my-prompt\n'
    result = _run_with_fake_claude(shell, script, state, bindir, record)
    assert result.returncode == 0, result.stderr

    argv = record.read_text().split("\n")
    assert "--session-id" in argv
    sid = argv[argv.index("--session-id") + 1]
    assert len(sid) == 36 and sid.count("-") == 4  # a real uuid was injected
    assert "my-prompt" in argv  # user args passed through


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_wrapper_ignores_flaglike_text_in_prompt(shell, tmp_path, capsys):
    # A prompt that merely CONTAINS "-r"/"--resume" as text is a fresh
    # launch and must still get an injected --session-id. (Regression: a
    # $*-flattening match treated prompt content as flags.)
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_bindir(tmp_path)
    record = tmp_path / "argv"
    script = f'source "{shim}"\nclaude "tell me about -r and --resume"\n'
    result = _run_with_fake_claude(shell, script, state, bindir, record)
    assert result.returncode == 0, result.stderr

    argv = record.read_text().split("\n")
    assert "--session-id" in argv, f"{shell}: flag-like prompt text suppressed sid injection"
    assert "tell me about -r and --resume" in argv  # prompt passed as one arg


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_wrapper_passes_resume_through_untouched(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_bindir(tmp_path)
    record = tmp_path / "argv"
    script = f'source "{shim}"\nclaude --resume abc123\n'
    result = _run_with_fake_claude(shell, script, state, bindir, record)
    assert result.returncode == 0, result.stderr

    argv = record.read_text().split("\n")
    assert "--session-id" not in argv  # resume must not get a fresh sid
    assert "--resume" in argv and "abc123" in argv


@pytest.mark.parametrize("shell", list(_SHELLS))
@pytest.mark.parametrize("cmdline", ["--resume abc123", "-r abc123", "--resume=abc123"])
def test_wrapper_journals_an_explicit_resume_sid(shell, cmdline, tmp_path, capsys):
    # A resumed session must be journaled so it is revivable. With no
    # transcript under the test HOME, an explicit sid is confidence 'guessed'
    # (nothing confirms it yet) — but the exact sid is recorded.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    claude = _resume_journal(shell, tmp_path, capsys, cmdline)
    assert claude is not None, f"{shell}: resume left the session untracked"
    assert claude["session_id"] == "abc123"
    assert claude["sid_source"] == "guessed"


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_wrapper_does_not_mistake_a_following_flag_for_the_sid(shell, tmp_path, capsys):
    # `claude -r --model foo`: -r has no sid value (--model is a flag), so no
    # explicit sid is extracted; with no transcript to guess, the session is
    # left untracked rather than journaling "--model" as the sid.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    claude = _resume_journal(shell, tmp_path, capsys, "-r --model foo")
    assert claude is None, f"{shell}: a following flag was wrongly taken as the sid"


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_wrapper_continue_without_transcript_stays_untracked(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    claude = _resume_journal(shell, tmp_path, capsys, "--continue")
    assert claude is None


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
