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
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# [#70] `pty`/`termios` are POSIX-only, and a module-level import runs at
# COLLECTION — before any skipif can fire. On Windows that turned a skip
# into a collection error. Skip the module first, then import.
if os.name != "posix":  # pragma: no cover - Windows CI
    pytest.skip("shim tests need a POSIX shell host", allow_module_level=True)

import pty  # noqa: E402  (deliberately after the platform guard)

from crr import cli
from crr.adapters import state_dir as state_dir_mod
from crr.core import contracts

_CRR_BIN = str(Path(sys.executable).parent / "crr")

# Per-shell: the argv to run a -c script with no user rc, and the token
# that expands to the shell's own pid.
_SHELLS = {
    "bash": {"argv": ["bash", "--norc", "--noprofile", "-c"], "pid": "$$"},
    "zsh": {"argv": ["zsh", "-f", "-c"], "pid": "$$"},
    "fish": {"argv": ["fish", "--no-config", "-c"], "pid": "$fish_pid"},
}

# Same invocations, plus -i so `status is-interactive` / `$- == *i*` /
# `-o interactive` read true even with no controlling tty.
_INTERACTIVE_ARGV = {
    "bash": ["bash", "--norc", "--noprofile", "-i", "-c"],
    "zsh": ["zsh", "-f", "-i", "-c"],
    "fish": ["fish", "--no-config", "-i", "-c"],
}

# [#43] Was Linux-only. macOS defaults to zsh and ships bash, so gating the
# WHOLE file on Linux meant the shims — the layer every session goes
# through — were never executed on the platform half the users are on. The
# gate is now what it always meant: a POSIX host with crr installed. Each
# test additionally skips the individual shells this host does not have
# (fish is commonly absent on a stock macOS runner), so a missing shell
# costs that shell's coverage rather than the file's.
pytestmark = pytest.mark.skipif(
    os.name != "posix" or not Path(_CRR_BIN).exists(),
    reason="needs a POSIX host + an installed crr console script",
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


def _shell_env(state_dir, **extra) -> dict:
    """Env for a shim subprocess, including where crr ACTUALLY stores state.

    CRR_STATE exists because the scripts used to hardcode
    $XDG_STATE_HOME/crr, which is simply wrong on macOS —
    ``state_dir.resolve`` sends Darwin to ~/Library/Application Support/crr
    and ignores XDG. The shims were writing to the right place; the
    assertions were reading a directory that only exists on Linux (#43).
    Built in one place so the five call sites cannot drift.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state_dir),
        "XDG_STATE_HOME": str(state_dir),
    }
    env["CRR_STATE"] = str(
        state_dir_mod.resolve(platform.system(), env, Path(env["HOME"]))
    )
    env.update(extra)
    return env


def _run(shell, script, state_dir) -> subprocess.CompletedProcess:
    env = _shell_env(state_dir)
    return subprocess.run(
        _SHELLS[shell]["argv"] + [script],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_shim_registers_a_valid_entry(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    pid = _SHELLS[shell]["pid"]
    script = f'source "{shim}"\ncat "$CRR_STATE/tabs/{pid}.json"\n'
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
    env = _shell_env(state_dir, PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                     CRR_TEST_RECORD=str(record))
    if journal is not None:
        env["CRR_TEST_JOURNAL"] = str(journal)
    return subprocess.run(
        _SHELLS[shell]["argv"] + [script],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
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
        'cat "$CRR_STATE/tabs/$PPID.json" > "$CRR_TEST_JOURNAL" 2>/dev/null\n'
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


# --- Remote Control: every claude launch crr is involved in enables it ---
#
# THE HAZARD: claude's `--remote-control` takes an OPTIONAL value, so a
# bare flag risks swallowing whatever follows it on the command line as
# the session name. crr always passes an explicit name (see
# crr.core.reviver.remote_control_flag_argv), printed by `crr
# remote-control-args` as one token per line so an UNQUOTED shell/fish
# command substitution splits it into exactly two argv words. That
# splitting behavior differs enough across bash/zsh/fish that it has to be
# proven empirically per shell (a text grep can't tell "--remote-control
# name" arrived as one word vs. two) — these two tests are the ones that
# discriminate; test_shim_enables_remote_control_on_every_launch_path
# below covers the remaining branches by presence, which is safe once the
# splitting itself is proven here (same shell, same substitution syntax).

def _remote_control_script(shim, proj_dir, cmdline):
    # cd BEFORE sourcing the shim so `register`'s $PWD (and therefore the
    # journaled cwd remote-control-args reads its name from) is the
    # controlled, all-safe-characters "proj" directory, not the test
    # runner's own (unpredictable) working directory.
    return f'cd "{proj_dir}"\nsource "{shim}"\nclaude {cmdline}\n'


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_wrapper_enables_remote_control_on_fresh_launch(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_bindir(tmp_path)
    record = tmp_path / "argv"
    proj = tmp_path / "proj"
    proj.mkdir()
    script = _remote_control_script(shim, proj, "my-prompt")
    result = _run_with_fake_claude(shell, script, state, bindir, record)
    assert result.returncode == 0, result.stderr

    argv = record.read_text().split("\n")
    assert "--remote-control" in argv, f"{shell}: fresh launch did not enable Remote Control"
    idx = argv.index("--remote-control")
    # The name must land as its OWN argv word — proves the shim's
    # unquoted-substitution splitting is correct in this shell, not merely
    # that the literal text "--remote-control" appears somewhere.
    assert argv[idx + 1] == "proj", f"{shell}: name did not arrive as a separate argv word: {argv}"


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_wrapper_enables_remote_control_on_explicit_resume(shell, tmp_path, capsys):
    # The top-of-wrapper passthrough branch (user typed --resume/--continue
    # themselves) — a different code shape than the fresh-launch branch
    # above, so it gets its own real-shell proof.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_bindir(tmp_path)
    record = tmp_path / "argv"
    proj = tmp_path / "proj"
    proj.mkdir()
    script = _remote_control_script(shim, proj, "--resume abc123")
    result = _run_with_fake_claude(shell, script, state, bindir, record)
    assert result.returncode == 0, result.stderr

    argv = record.read_text().split("\n")
    assert "--resume" in argv and "abc123" in argv  # untouched
    assert "--remote-control" in argv
    idx = argv.index("--remote-control")
    assert argv[idx + 1] == "proj", f"{shell}: name did not arrive as a separate argv word: {argv}"


def _shim_text(name):
    from importlib import resources
    return resources.files("crr.shims").joinpath(name).read_text(encoding="utf-8")


@pytest.mark.parametrize("shim", ["crr.bash", "crr.zsh", "crr.fish"])
def test_shim_enables_remote_control_on_every_launch_path(shim):
    # Six `command claude` invocations per shim: the top-level resuming
    # passthrough, the fresh launch's --session-id branch and its no-sid
    # fallback, and the repair loop's three relaunch paths (silent kick,
    # crash-retry with a known sid, crash-retry via --continue). Every one
    # has to ask for and append the Remote Control args, or a
    # revived/relaunched/resumed session comes back unreachable from the
    # phone (the Goal this whole feature exists for).
    text = _shim_text(shim)
    assert "remote-control-args --pid" in text
    invocations = [line for line in text.splitlines() if "command claude" in line]
    assert len(invocations) == 6, invocations
    assert all("_crr_rc_args" in line for line in invocations), invocations


_RESUME_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


@pytest.mark.parametrize("shell", list(_SHELLS))
@pytest.mark.parametrize("cmdline", [f"--resume {_RESUME_SID}", f"-r {_RESUME_SID}",
                                     f"--resume={_RESUME_SID}"])
def test_wrapper_journals_an_explicit_resume_sid(shell, cmdline, tmp_path, capsys):
    # A resumed session must be journaled so it is revivable. With no
    # transcript under the test HOME, an explicit sid is confidence 'guessed'
    # (nothing confirms it yet) — but the exact sid is recorded.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    claude = _resume_journal(shell, tmp_path, capsys, cmdline)
    assert claude is not None, f"{shell}: resume left the session untracked"
    assert claude["session_id"] == _RESUME_SID
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
        'cat "$CRR_STATE/tabs/$$.json"\n'
    )
    result = _run("bash", script, state)
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)
    assert entry["last_cmd"] != ""


# ---------------------------------------------------------------------------
# Repair loop (Slice 2b) — the claude() wrapper's post-exit flag branching.
# Each test runs a real shell; the offer-path tests drive a pty. Gated per
# installed shell like everything above. _REPAIR_SHELLS grows as each shell's
# loop is implemented (fish → bash → zsh); _TIMED_SHELLS lists the shells
# with a timed read (bash/zsh — fish blocks and relies on the no-tty rule).
# ---------------------------------------------------------------------------

_REPAIR_SHELLS = ["fish", "bash", "zsh"]
_TIMED_SHELLS = ["bash", "zsh"]


def _fake_claude_repair_bindir(tmp_path) -> Path:
    """A fake `claude` for repair-loop tests.

    Appends each call's argv (space-joined, one line) to CRR_TEST_RECORD,
    exits with the n-th code of CRR_TEST_EXITS (last repeats), and on the
    FIRST call only, arms CRR_TEST_FLAG (raw flag-file content) for its
    parent — the wrapper's shell — mid-run, i.e. after the wrapper's
    stale-flag clear, exactly when a real kick/close would land.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$CRR_TEST_RECORD"\n'
        "n=0\n"
        '[ -f "$CRR_TEST_COUNT" ] && n=$(cat "$CRR_TEST_COUNT")\n'
        'n=$((n+1)); echo "$n" > "$CRR_TEST_COUNT"\n'
        'if [ "$n" -eq 1 ] && [ -n "$CRR_TEST_FLAG" ]; then\n'
        '  mkdir -p "$CRR_STATE/relaunch"\n'
        '  printf %s "$CRR_TEST_FLAG" > "$CRR_STATE/relaunch/$PPID"\n'
        "fi\n"
        "codes=($CRR_TEST_EXITS)\n"
        "idx=$((n-1))\n"
        '[ "$idx" -ge "${#codes[@]}" ] && idx=$((${#codes[@]} - 1))\n'
        'exit "${codes[$idx]}"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return bindir


def _repair_env(state_dir, bindir, tmp_path, exits, flag=None, extra=None):
    env = _shell_env(
        state_dir,
        PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        CRR_TEST_RECORD=str(tmp_path / "record"),
        CRR_TEST_COUNT=str(tmp_path / "count"),
        CRR_TEST_EXITS=exits,
    )
    if flag is not None:
        env["CRR_TEST_FLAG"] = flag
    if extra:
        env.update(extra)
    return env


def _repair_script(shell, shim, cmdline="go", pre="", marker=True):
    """Shell script: source the shim, record the shell pid, run claude.
    AFTER-MARKER proves the shell survived (close must NOT print it)."""
    pid = _SHELLS[shell]["pid"]
    lines = [f'source "{shim}"', f'echo {pid} > "$XDG_STATE_HOME/shellpid"']
    if pre:
        lines.append(pre)
    lines.append(f"claude {cmdline}")
    if marker:
        lines.append("echo AFTER-MARKER")
    return "\n".join(lines) + "\n"


def _skip_fish_pty_on_macos(shell: str) -> None:
    """[#43, unresolved] fish's `read` never returns under a pty on macOS CI.

    Both crash-offer tests time out after 30s there, and only for fish —
    bash and zsh pass, and all three pass on Linux with fish 3.7.1. The
    cause is not established: brew ships a newer fish than the Linux runner,
    and the pty write may also race differently on Darwin. Skipped rather
    than guessed at, because the two candidate explanations lead to
    opposite fixes and neither can be tested without the platform.

    This is NOT a claim that the fish shim works on macOS. It is a claim
    that this harness cannot currently tell.
    """
    if shell == "fish" and platform.system() == "Darwin":
        pytest.skip("fish read under a pty hangs on macOS CI — unresolved, see #43")


def _run_pty(argv, env, input_bytes, timeout=30):
    """Run argv with stdin on a pty slave (so `test -t 0` is true), with
    input_bytes pre-buffered in the pty. stdout/stderr stay pipes."""
    master, slave = pty.openpty()
    try:
        if input_bytes:
            os.write(master, input_bytes)
        proc = subprocess.Popen(
            argv, env=env, stdin=slave,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        os.close(slave)
        slave = -1
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    finally:
        if slave != -1:
            os.close(slave)
        os.close(master)


def _record_lines(tmp_path):
    rec = tmp_path / "record"
    return rec.read_text(encoding="utf-8").splitlines() if rec.exists() else []


_OFFER = "Resume this conversation?"


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_stale_flag_cleared_at_wrapper_start(shell, tmp_path, capsys):
    # A flag armed BEFORE the wrapper starts must never act on this launch.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    pid = _SHELLS[shell]["pid"]
    pre = (
        'mkdir -p "$CRR_STATE/relaunch"\n'
        f'printf "relaunch stale-sid" > "$CRR_STATE/relaunch/{pid}"'
    )
    script = _repair_script(shell, shim, pre=pre)
    env = _repair_env(state, bindir, tmp_path, exits="0")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(_record_lines(tmp_path)) == 1  # never resumed the stale sid
    assert "AFTER-MARKER" in result.stdout
    recorded = (state / "shellpid").read_text().strip()
    assert not (state / "crr" / "relaunch" / recorded).exists()


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_relaunch_flag_resumes_silently(shell, tmp_path, capsys):
    # kick: claude dies 143 with a relaunch flag → silent `--resume <sid>`.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="143 0",
                      flag="relaunch test-sid-123")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = _record_lines(tmp_path)
    assert len(lines) == 2
    # startswith, not ==: the relaunch also carries the appended Remote
    # Control args (see the "Remote Control" test block below) — this test
    # is about the relaunch sid, not that.
    assert lines[1].startswith("--resume test-sid-123")
    assert _OFFER not in result.stderr  # silent — no offer on a kick
    assert "AFTER-MARKER" in result.stdout  # shell survives a kick
    recorded = (state / "shellpid").read_text().strip()
    assert not (state / "crr" / "relaunch" / recorded).exists()  # consumed


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_close_flag_exits_the_shell(shell, tmp_path, capsys):
    # close: claude dies 143 with a close flag → claude-exit, then the
    # wrapper exits the whole shell (AFTER-MARKER unreachable) and the exit
    # hook deregisters the journal entry.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="143", flag="close")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert len(_record_lines(tmp_path)) == 1  # close never relaunches
    assert "AFTER-MARKER" not in result.stdout
    recorded = (state / "shellpid").read_text().strip()
    assert not (state / "crr" / "tabs" / f"{recorded}.json").exists()


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_unknown_flag_kind_treated_as_absent(shell, tmp_path, capsys):
    # Hard requirement 1: a stale/foreign kind (e.g. Slice-1 bare sid) must
    # fall through to normal exit handling, never act.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="0", flag="defer xyz")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(_record_lines(tmp_path)) == 1
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_relaunch_without_sid_treated_as_absent(shell, tmp_path, capsys):
    # Hard requirement 2: never `claude --resume` with an empty argument.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="0", flag="relaunch")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(_record_lines(tmp_path)) == 1
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_crash_without_tty_resumes_with_known_sid(shell, tmp_path, capsys):
    # Bare crash, stdin not a tty → resume immediately, no prompt, using the
    # sid injected at launch.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="7 0")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = _record_lines(tmp_path)
    assert len(lines) == 2
    first = lines[0].split()
    sid = first[first.index("--session-id") + 1]
    assert lines[1].startswith(f"--resume {sid}")  # + Remote Control args
    assert _OFFER not in result.stderr  # no tty → no prompt
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_crash_resume_capped_at_two_attempts(shell, tmp_path, capsys):
    # A session that keeps dying gives up in place: initial run + 2 resumes.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="7")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(_record_lines(tmp_path)) == 3
    assert "AFTER-MARKER" in result.stdout  # gave up, shell back at prompt


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_crash_offer_explicit_no_declines(shell, tmp_path, capsys):
    # With a tty, an explicit `n` at the offer stops the loop.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    _skip_fish_pty_on_macos(shell)
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="7")
    result = _run_pty(_SHELLS[shell]["argv"] + [script], env, b"n\n")
    assert len(_record_lines(tmp_path)) == 1  # declined — no resume
    assert _OFFER in result.stderr
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_crash_offer_yes_resumes(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    _skip_fish_pty_on_macos(shell)
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="7 0")
    result = _run_pty(_SHELLS[shell]["argv"] + [script], env, b"y\n")
    lines = _record_lines(tmp_path)
    assert len(lines) == 2
    assert lines[1].startswith("--resume ")
    assert _OFFER in result.stderr
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_crash_with_unknown_sid_falls_back_to_continue(shell, tmp_path, capsys):
    # `claude --continue` with no transcript stays untracked (no _cur_sid);
    # a crash then resumes via `--continue` (decision 3), not `--resume `.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim, cmdline="--continue")
    env = _repair_env(state, bindir, tmp_path, exits="7 0")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = _record_lines(tmp_path)
    assert len(lines) == 2
    assert lines[1].startswith("--continue")  # + Remote Control args
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _TIMED_SHELLS)
def test_repair_crash_offer_timeout_resumes(shell, tmp_path, capsys):
    # bash/zsh timed read: no answer within $_CRR_OFFER_TIMEOUT → resume.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="7 0",
                      extra={"_CRR_OFFER_TIMEOUT": "1"})
    result = _run_pty(_SHELLS[shell]["argv"] + [script], env, b"")
    lines = _record_lines(tmp_path)
    assert len(lines) == 2
    assert lines[1].startswith("--resume ")
    assert _OFFER in result.stderr
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_loop_is_inert_when_crr_binary_is_absent(shell, tmp_path, capsys):
    # A shim baked with a nonexistent crr must behave exactly like the
    # pre-loop wrapper on a crash: one launch, no offer, no resume.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    assert cli.main(["shim", shell, "--crr-bin", "/nonexistent/crr"]) == 0
    shim = tmp_path / f"crr.{shell}"
    shim.write_text(capsys.readouterr().out, encoding="utf-8")
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="7")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(_record_lines(tmp_path)) == 1
    assert _OFFER not in result.stderr
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_crash_after_kick_resumes_the_kicked_sid(shell, tmp_path, capsys):
    # A relaunch flag updates _cur_sid, so a later crash resumes the KICKED
    # sid (and the relaunch reset the crash cap — decision 2 and 3).
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="143 7 0",
                      flag="relaunch kicked-sid-xyz")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = _record_lines(tmp_path)
    assert len(lines) == 3
    assert lines[1].startswith("--resume kicked-sid-xyz")  # + Remote Control args
    assert lines[2].startswith("--resume kicked-sid-xyz")
    assert "AFTER-MARKER" in result.stdout


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_close_flag_wins_over_clean_exit(shell, tmp_path, capsys):
    # close is checked before the exit code: a clean exit with a close flag
    # still closes the shell.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_repair_bindir(tmp_path)
    script = _repair_script(shell, shim)
    env = _repair_env(state, bindir, tmp_path, exits="0", flag="close")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    assert len(_record_lines(tmp_path)) == 1
    assert "AFTER-MARKER" not in result.stdout


# ---------------------------------------------------------------------------
# Phase-3 restore prompt — `crr rescue-check` invoked from each shim.
# Contract tests are string-level and ungated (no shell binary needed to
# generate/inspect the shim text). Behavior tests source a real shim under
# each installed shell, with a fake `crr` that logs rescue-check calls.
# ---------------------------------------------------------------------------

# Exact block each shim must emit immediately after its `register` call.
# stdout is left attached (the [Y/n] prompt must reach the user); only
# stderr is silenced.
_RESCUE_CHECK_BLOCK = {
    "fish": re.compile(
        r'if status is-interactive; and test -x "\$_CRR_BIN"\n'
        r'\s*"\$_CRR_BIN" rescue-check 2>/dev/null\n'
        r'end'
    ),
    "bash": re.compile(
        r'if \[\[ \$- == \*i\* && -x "\$_CRR_BIN" \]\]; then\n'
        r'\s*"\$_CRR_BIN" rescue-check 2>/dev/null\n'
        r'fi'
    ),
    "zsh": re.compile(
        r'if \[\[ -o interactive && -x "\$_CRR_BIN" \]\]; then\n'
        r'\s*"\$_CRR_BIN" rescue-check 2>/dev/null\n'
        r'fi'
    ),
}


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_shim_rescue_check_is_guarded_and_stdout_stays_open(shell, tmp_path, capsys):
    shim = _make_shim(shell, tmp_path, capsys)
    text = shim.read_text(encoding="utf-8")
    assert _RESCUE_CHECK_BLOCK[shell].search(text), text

    line = next(l for l in text.splitlines() if "rescue-check" in l)
    assert line.rstrip().endswith("2>/dev/null")
    # Only the stderr redirect — no bare `>/dev/null` swallowing stdout.
    assert line.count(">/dev/null") == 1


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_shim_rescue_check_placed_right_after_register(shell, tmp_path, capsys):
    shim = _make_shim(shell, tmp_path, capsys)
    lines = shim.read_text(encoding="utf-8").splitlines()
    register_idx = next(i for i, l in enumerate(lines) if " register --pid " in l)
    rescue_idx = next(i for i, l in enumerate(lines) if "rescue-check" in l)
    # Allow the one-line comment the brief's snippet adds before the guard,
    # but nothing else (no other statement) in between.
    between = lines[register_idx + 1:rescue_idx]
    assert all(l.strip() == "" or l.strip().startswith("#") or l.strip().startswith("if ")
               for l in between), lines[register_idx:rescue_idx + 2]


def _fake_crr_bin(tmp_path) -> Path:
    """A fake `crr` binary for the rescue-check hook: logs an invocation
    whenever it is called with `rescue-check` as the first argument, and
    no-ops (exit 0, no output) for every other subcommand the shim's own
    `_crr` helper fires (register/deregister/etc.) while sourcing."""
    fake = tmp_path / "fake-crr"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = "rescue-check" ] && echo called >> "$CRR_TEST_RECORD"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _rescue_check_env(state_dir, record) -> dict:
    return _shell_env(state_dir, CRR_TEST_RECORD=str(record))


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_rescue_check_runs_on_interactive_shell(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    fake_crr = _fake_crr_bin(tmp_path)
    assert cli.main(["shim", shell, "--crr-bin", str(fake_crr)]) == 0
    shim = tmp_path / f"crr.{shell}"
    shim.write_text(capsys.readouterr().out, encoding="utf-8")
    state = tmp_path / "state"
    record = tmp_path / "record"
    script = f'source "{shim}"\n'
    result = subprocess.run(
        _INTERACTIVE_ARGV[shell] + [script], env=_rescue_check_env(state, record),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert record.exists() and "called" in record.read_text(), \
        f"{shell}: interactive shell never ran rescue-check ({result.stderr})"


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_rescue_check_skipped_on_noninteractive_shell(shell, tmp_path, capsys):
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    fake_crr = _fake_crr_bin(tmp_path)
    assert cli.main(["shim", shell, "--crr-bin", str(fake_crr)]) == 0
    shim = tmp_path / f"crr.{shell}"
    shim.write_text(capsys.readouterr().out, encoding="utf-8")
    state = tmp_path / "state"
    record = tmp_path / "record"
    script = f'source "{shim}"\n'
    result = subprocess.run(
        _SHELLS[shell]["argv"] + [script], env=_rescue_check_env(state, record),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not record.exists(), f"{shell}: non-interactive shell ran rescue-check"


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_rescue_check_is_silent_no_op_when_crr_binary_is_absent(shell, tmp_path, capsys):
    # Missing crr = silent no-op, even on an interactive shell where the
    # `-x` guard would otherwise let the invocation through. Interactive
    # shells emit their own unrelated stderr noise (no controlling tty:
    # "no job control", fish's "Could not set up terminal" TERM warning) —
    # asserted against below, not "stderr is empty".
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    assert cli.main(["shim", shell, "--crr-bin", "/nonexistent/crr"]) == 0
    shim = tmp_path / f"crr.{shell}"
    shim.write_text(capsys.readouterr().out, encoding="utf-8")
    state = tmp_path / "state"
    script = f'source "{shim}"\n'
    result = subprocess.run(
        _INTERACTIVE_ARGV[shell] + [script],
        env=_shell_env(state),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "/nonexistent/crr" not in result.stderr
    assert "rescue-check" not in result.stderr


# --- the two-agents guard actually gates the launch (#48, #68) ------------
#
# The layer that matters here is the shim, not the CLI: `crr conflict-check`
# returning non-zero proves nothing on its own — what has to hold is that
# claude never starts. Both call sites are exercised by swapping _CRR_BIN
# for a stub after sourcing, so the decision can be forced without needing
# a real live claude process to conflict with.

def _stub_crr(tmp_path, name, conflict_exit) -> Path:
    """A stand-in crr that exits `conflict_exit` for conflict-check.

    Everything else answers 0 with no output, which is what the wrapper's
    other calls (register, claude-resume, remote-control-args,
    repair-check) already treat as "nothing to do".
    """
    stub = tmp_path / name
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = conflict-check ]; then\n'
        f'  printf "%s\\n" "$@" >> "$CRR_TEST_CONFLICT"\n'
        f"  exit {conflict_exit}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run_guarded(shell, tmp_path, capsys, cmdline, conflict_exit):
    """Run `claude <cmdline>` with conflict-check forced to `conflict_exit`.

    Returns (claude_argv_or_None, conflict_check_argv_text).
    """
    shim = _make_shim(shell, tmp_path, capsys)
    state = tmp_path / "state"
    bindir = _fake_claude_bindir(tmp_path)
    record = tmp_path / "argv"
    conflict = tmp_path / "conflict"
    stub = _stub_crr(tmp_path, "stub-crr", conflict_exit)
    setter = ("set -g _CRR_BIN" if shell == "fish" else "_CRR_BIN=")
    joiner = " " if shell == "fish" else ""
    script = (f'source "{shim}"\n'
              f'{setter}{joiner}"{stub}"\n'
              f"claude {cmdline}\n")
    env_extra = {"CRR_TEST_CONFLICT": str(conflict)}
    env = _shell_env(state, PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                     CRR_TEST_RECORD=str(record), **env_extra)
    result = subprocess.run(
        _SHELLS[shell]["argv"] + [script],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    argv = record.read_text().split("\n") if record.exists() else None
    asked = conflict.read_text() if conflict.exists() else ""
    return argv, asked, result


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_a_refused_continue_never_starts_claude(shell, tmp_path, capsys):
    # #68's whole point. Exit 3 is conflict-check's refusal; the wrapper
    # must abandon the launch, not merely report it.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    argv, asked, result = _run_guarded(shell, tmp_path, capsys, "--continue", 3)
    assert "--cwd" in asked, f"{shell}: --continue was not conflict-checked"
    assert argv is None, f"{shell}: claude STARTED despite a refusal ({argv})"
    assert result.returncode != 0


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_a_refused_explicit_resume_never_starts_claude(shell, tmp_path, capsys):
    # The #48 path, which had no shim-level test at all — the CLI was
    # covered, the gate it is supposed to operate was not.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    argv, asked, result = _run_guarded(shell, tmp_path, capsys, f"--resume {sid}", 3)
    assert f"--sid\n{sid}" in asked, f"{shell}: the sid was not checked"
    assert argv is None, f"{shell}: claude STARTED despite a refusal ({argv})"
    assert result.returncode != 0


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_a_cleared_continue_launches_normally(shell, tmp_path, capsys):
    # The control case: exit 0 must not become an accidental block.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    argv, asked, result = _run_guarded(shell, tmp_path, capsys, "--continue", 0)
    assert "--cwd" in asked
    assert argv is not None and "--continue" in argv, result.stderr


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_an_older_crr_that_cannot_answer_does_not_block_the_launch(shell, tmp_path, capsys):
    # A shim regenerated ahead of `crr deploy` passes --cwd to a crr that
    # has never heard of it; argparse exits 2. Treating "could not answer"
    # as "refused" would make --continue permanently unusable, and _crr
    # swallows stderr, so the user would get no explanation. The standing
    # shim contract is that crr can never break a launch — the conflict
    # card still catches this after the fact.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    argv, asked, result = _run_guarded(shell, tmp_path, capsys, "--continue", 2)
    assert "--cwd" in asked
    assert argv is not None and "--continue" in argv, (
        f"{shell}: an unanswerable check blocked the launch: {result.stderr}"
    )


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_a_bare_resume_picker_is_not_conflict_checked(shell, tmp_path, capsys):
    # `claude --resume` with no sid opens claude's interactive picker. The
    # user may choose any conversation, so predicting "the newest" and
    # forcing a kill choice about it would be wrong. Left to the post-hoc
    # conflict card, deliberately — this test is the record of that choice.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
    argv, asked, result = _run_guarded(shell, tmp_path, capsys, "--resume", 3)
    assert asked == "", f"{shell}: the picker path was conflict-checked: {asked!r}"
    assert argv is not None and "--resume" in argv, result.stderr
