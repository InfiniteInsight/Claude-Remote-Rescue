# Shim Repair Loop (Task #4 Slice 2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `claude()` wrapper in all three shims (fish/bash/zsh) a post-exit repair loop that consumes the 3-state relaunch/close flag: kick → silent resume, close → deregister + exit the shell, bare crash → offer to resume, clean exit → return to prompt.

**Architecture:** Pure shell additions to `crr/shims/crr.{fish,bash,zsh}` — no Python changes. The wrapper already journals launches/resumes; it gains (a) a stale-flag clear at start, (b) an exit-code capture, (c) a `while` loop that reads `crr repair-check --pid <pid>`, clears it, and branches on the parsed kind. Tests are real-shell subprocess tests in `tests/test_shims.py`, mirroring the existing per-shell gating; offer-path tests drive a pty.

**Tech Stack:** fish/bash/zsh shell code; pytest + Python `pty` module for the tty-dependent tests.

**Authoritative spec:** `docs/superpowers/specs/2026-07-27-kick-close-repair-loop-design.md` — sections "The flag protocol (3-state)", "Shim repair loop", the four numbered **Slice-2b hard requirements**, and "Slice-2b resolved design decisions" (decisions 1–6). The four hard requirements, restated:

1. Unknown flag kind → treat as absent (never act on it).
2. `relaunch` with no sid → treat as absent (never `--resume` with an empty arg).
3. Parse the flag line yourself (fish `string split ' '`; bash/zsh `read kind sid` via herestring). Absent = empty output.
4. Read and clear are two separate `crr` calls, immediately adjacent (read-then-clear; the small re-arm window is accepted by design).

## Global Constraints

- Python 3.12, **zero runtime dependencies**; shims are dependency-free shell that only shells out to `crr` via the `_crr` helper (silent no-op if the binary is missing).
- One-way layering `crr.cli → crr.adapters → crr.core`; `.venv/bin/lint-imports` must print `KEPT` (no Python changes here, but the gate runs anyway).
- TDD: write the failing test, watch it fail for the right reason, then implement.
- Local CI = `.venv/bin/pytest -q` + `.venv/bin/lint-imports`. GitHub Actions is billing-blocked; never rely on it.
- No `page.html` change in this slice → no `PAGE_VERSION` bump.
- Cross-OS/shell parity: all three shims change in this branch. zsh may not be installed on the dev machine — its tests gate/skip exactly like the existing `_installed()` pattern; the code ships regardless.
- Absolute safety rules of `docs/HANDOFF-remaining-work.md` §0 apply to every step: never touch the `cc-*` tmux sessions, ccresume, or port 8377; all live testing is isolated (scratch `XDG_STATE_HOME`, scratch `TMUX_TMPDIR`, fake `claude`, `fish --no-config`); detect strays with `ps -C sleep`, never `pkill -f`.
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## File Structure

- Modify: `crr/shims/crr.fish` — rewrite the `claude` function only.
- Modify: `crr/shims/crr.bash` — rewrite the `claude` function only.
- Modify: `crr/shims/crr.zsh` — rewrite the `claude` function only.
- Modify: `tests/test_shims.py` — append a "Repair loop (Slice 2b)" section: shared fake-claude + pty helpers and ~10 parametrized tests.
- Create: `docs/superpowers/smoke/2026-07-27-slice2b-live-smoke.md` — Task 4's isolated live-smoke report (main session only).

## Shared wrapper behavior (all three shells; the single design every task translates)

```
claude():
  clear any stale flag: _crr repair-check --pid <pid> --clear   # spec step 1
  <existing arg parsing and journaling, unchanged>
  _cur_sid = injected sid (fresh launch) | explicit sid (resume) | ""   # decision 3
  run the initial `command claude ...` exactly as today; capture exit code
  _crashes = 0
  loop forever:
    flagline = `_crr repair-check --pid <pid>`     # read...
    _crr repair-check --pid <pid> --clear          # ...then clear (hard req 4)
    parse kind, fsid from flagline                  # hard req 3
    if kind == "relaunch" and fsid nonempty:        # hard req 2 guards empty fsid
        _cur_sid = fsid; _crashes = 0               # decision 2: kick resets the cap
        run `command claude --resume <fsid>`; capture code; continue
    if kind == "close":
        _crr claude-exit --pid <pid>; exit          # terminal; closes the shell
    # unknown kind falls through here == absent     # hard req 1
    if code == 0: break                             # clean exit → prompt
    if _crashes >= 2: break                         # give-up cap (crash branch only)
    ans = ""
    if stdin is a tty:                              # decision 4
        print "crr: claude exited unexpectedly (<code>). Resume this conversation? [Y/n] " to stderr
        timed read (bash/zsh, $_CRR_OFFER_TIMEOUT default 30) / blocking read (fish)
    if ans is n/N/no/No/NO: break                   # only explicit no declines
    _crashes += 1
    if _cur_sid nonempty: run `command claude --resume <_cur_sid>`; capture code
    else: _crr claude-resume --pid <pid> --cwd <cwd>; run `command claude --continue`; capture code   # decision 3
  _crr claude-exit --pid <pid>                      # decision 1: all terminal states
```

---

### Task 1: Repair-loop test scaffolding + fish implementation

**Files:**
- Modify: `tests/test_shims.py` (append after `test_bash_shim_records_last_cmd`)
- Modify: `crr/shims/crr.fish` (the `claude` function)

**Interfaces:**
- Produces: `_REPAIR_SHELLS` (list, starts `["fish"]`), `_TIMED_SHELLS` (list, starts `[]`), `_fake_claude_repair_bindir(tmp_path)`, `_repair_env(state_dir, bindir, tmp_path, exits, flag=None, extra=None)`, `_repair_script(shell, shim, cmdline="go", marker=True)`, `_run_pty(argv, env, input_bytes, timeout=30)`, `_record_lines(tmp_path)`. Tasks 2–3 only append shell names to the two lists and reuse everything.

- [ ] **Step 1: Add the test scaffolding + fish-parametrized failing tests**

Append to `tests/test_shims.py` (add `import pty` next to the existing imports at the top of the file):

```python
# ---------------------------------------------------------------------------
# Repair loop (Slice 2b) — the claude() wrapper's post-exit flag branching.
# Each test runs a real shell; the offer-path tests drive a pty. Gated per
# installed shell like everything above. _REPAIR_SHELLS grows as each shell's
# loop is implemented (fish → bash → zsh); _TIMED_SHELLS lists the shells
# with a timed read (bash/zsh — fish blocks and relies on the no-tty rule).
# ---------------------------------------------------------------------------

_REPAIR_SHELLS = ["fish"]
_TIMED_SHELLS: list = []


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
        '  mkdir -p "$XDG_STATE_HOME/crr/relaunch"\n'
        '  printf %s "$CRR_TEST_FLAG" > "$XDG_STATE_HOME/crr/relaunch/$PPID"\n'
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
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(state_dir),
        "XDG_STATE_HOME": str(state_dir),
        "CRR_TEST_RECORD": str(tmp_path / "record"),
        "CRR_TEST_COUNT": str(tmp_path / "count"),
        "CRR_TEST_EXITS": exits,
    }
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
        'mkdir -p "$XDG_STATE_HOME/crr/relaunch"\n'
        f'printf "relaunch stale-sid" > "$XDG_STATE_HOME/crr/relaunch/{pid}"'
    )
    script = _repair_script(shell, shim, pre=pre)
    env = _repair_env(state, bindir, tmp_path, exits="0")
    result = subprocess.run(_SHELLS[shell]["argv"] + [script], env=env,
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
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = _record_lines(tmp_path)
    assert len(lines) == 2
    assert lines[1] == "--resume test-sid-123"
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
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = _record_lines(tmp_path)
    assert len(lines) == 2
    first = lines[0].split()
    sid = first[first.index("--session-id") + 1]
    assert lines[1] == f"--resume {sid}"
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
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(_record_lines(tmp_path)) == 3
    assert "AFTER-MARKER" in result.stdout  # gave up, shell back at prompt


@pytest.mark.parametrize("shell", _REPAIR_SHELLS)
def test_repair_crash_offer_explicit_no_declines(shell, tmp_path, capsys):
    # With a tty, an explicit `n` at the offer stops the loop.
    if not _installed(shell):
        pytest.skip(f"{shell} not installed")
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
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = _record_lines(tmp_path)
    assert len(lines) == 2
    assert lines[1] == "--continue"
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
```

- [ ] **Step 2: Run the fish tests, verify they fail for the right reason**

Run: `.venv/bin/pytest tests/test_shims.py -k repair -v`
Expected: the fish-parametrized tests FAIL (e.g. relaunch test: record has 1 line, no `--resume`; close test: AFTER-MARKER printed). `_TIMED_SHELLS` is empty so the timeout test collects nothing — that is expected at this stage. If fish is not installed the whole set skips — on this machine fish IS installed, so failures must appear.

- [ ] **Step 3: Rewrite the `claude` function in `crr/shims/crr.fish`**

Replace the entire `function claude` … `end` block (keep everything above it unchanged) with:

```fish
# claude() wrapper: inject + journal a --session-id on fresh launches
# (sid_source=injected); on resume/continue, journal the resumed session
# (guessed/verified sid) so it is revivable, args untouched. After each
# exit the repair loop consumes the relaunch/close flag: kick → silent
# --resume, close → deregister + exit this shell, bare crash → offer
# ([Y/n]; yes/no-tty resume, ≤2 attempts), clean exit → back to prompt.
function claude
    # [lesson: flag files] A stale flag from a prior action must never act
    # on this launch.
    _crr repair-check --pid $fish_pid --clear >/dev/null

    # Element-wise flag detection: whole arguments only, never a substring
    # of prompt text (a prompt like "explain -r" is a fresh launch).
    set -l _resuming 0
    for _arg in $argv
        switch $_arg
            case -r --resume '--resume=*' -c --continue --session-id '--session-id=*'
                set _resuming 1
                break
        end
    end
    # The conversation the repair loop resumes: injected sid on a fresh
    # launch, explicit sid on a resume, sid of each consumed relaunch flag.
    set -l _cur_sid ""
    if test $_resuming -eq 1
        # Extract an explicit resume sid if given (-r <sid>, --resume <sid|=sid>,
        # --session-id <sid|=sid>); a '-'-prefixed value is another flag, not the
        # sid, so it is left empty and the sid is guessed from the newest transcript.
        set -l _sid ""
        set -l _want 0
        for _arg in $argv
            if test $_want -eq 1
                switch $_arg
                    case '-*'
                    case '*'
                        set _sid $_arg
                end
                set _want 0
                continue
            end
            switch $_arg
                case -r --resume --session-id
                    set _want 1
                case '--resume=*'
                    set _sid (string replace -- '--resume=' '' $_arg)
                case '--session-id=*'
                    set _sid (string replace -- '--session-id=' '' $_arg)
            end
        end
        if test -n "$_sid"
            _crr claude-resume --pid $fish_pid --cwd $PWD --session-id $_sid >/dev/null
            set _cur_sid $_sid
        else
            _crr claude-resume --pid $fish_pid --cwd $PWD >/dev/null
        end
        command claude $argv
    else
        set -l _crr_sid (_crr claude-launch --pid $fish_pid)
        if test -n "$_crr_sid"
            set _cur_sid $_crr_sid
            command claude --session-id $_crr_sid $argv
        else
            command claude $argv
        end
    end
    set -l _code $status

    set -l _crashes 0
    while true
        # Command substitution splits on newlines only — split the single
        # flag line on spaces ourselves. Absent = no output = empty list.
        # Read, then clear: two calls by design (re-arm window accepted).
        set -l _flag (_crr repair-check --pid $fish_pid | string split ' ')
        _crr repair-check --pid $fish_pid --clear >/dev/null
        # Quoted out-of-range index expands empty, so a bare "relaunch"
        # (no sid) safely falls through to the absent branches below.
        if test "$_flag[1]" = relaunch; and test -n "$_flag[2]"
            set _cur_sid $_flag[2]
            set _crashes 0
            command claude --resume $_flag[2]
            set _code $status
            continue
        end
        if test "$_flag[1]" = close
            _crr claude-exit --pid $fish_pid
            exit
        end
        # Unknown kind or no flag: branch on how claude exited.
        if test $_code -eq 0
            break
        end
        if test $_crashes -ge 2
            break
        end
        set -l _ans ""
        if test -t 0
            printf 'crr: claude exited unexpectedly (%s). Resume this conversation? [Y/n] ' $_code >&2
            # fish has no timed read — block; the no-tty guard above covers
            # the unattended case.
            read _ans
        end
        switch $_ans
            case n N no No NO
                break
        end
        set _crashes (math $_crashes + 1)
        if test -n "$_cur_sid"
            command claude --resume $_cur_sid
        else
            _crr claude-resume --pid $fish_pid --cwd $PWD >/dev/null
            command claude --continue
        end
        set _code $status
    end
    _crr claude-exit --pid $fish_pid
end
```

- [ ] **Step 4: Run the fish tests, verify they pass**

Run: `.venv/bin/pytest tests/test_shims.py -k repair -v`
Expected: all fish-parametrized repair tests PASS.

- [ ] **Step 5: Run the full suite + layering gate**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: all tests pass (the pre-existing fish wrapper tests must still pass — the loop must not change clean-exit behavior), `KEPT` printed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shims.py crr/shims/crr.fish
git commit -m "feat(shims): fish repair loop — relaunch/close flag branching after claude exits"
```

---

### Task 2: bash implementation

**Files:**
- Modify: `tests/test_shims.py` (two list literals only)
- Modify: `crr/shims/crr.bash` (the `claude` function)

**Interfaces:**
- Consumes: all Task-1 helpers unchanged.
- Produces: `_REPAIR_SHELLS = ["fish", "bash"]`, `_TIMED_SHELLS = ["bash"]`.

- [ ] **Step 1: Enable bash in the repair tests**

In `tests/test_shims.py` change the two lists:

```python
_REPAIR_SHELLS = ["fish", "bash"]
_TIMED_SHELLS = ["bash"]
```

- [ ] **Step 2: Run, verify the bash params fail for the right reason**

Run: `.venv/bin/pytest tests/test_shims.py -k "repair and bash" -v`
Expected: FAIL (no loop in the bash wrapper yet); the timeout test now collects for bash and fails.

- [ ] **Step 3: Rewrite the `claude` function in `crr/shims/crr.bash`**

Replace the entire `claude() { … }` block (keep everything above it unchanged) with:

```bash
# claude() wrapper: on a fresh launch, inject a --session-id so the session
# is identifiable and journal it (sid_source=injected). On resume/continue,
# journal the resumed session too (guessed/verified sid) so it is revivable.
# After each exit the repair loop consumes the relaunch/close flag: kick →
# silent --resume, close → deregister + exit this shell, bare crash → offer
# ([Y/n]; yes/timeout/no-tty resume, ≤2 attempts), clean exit → prompt.
claude() {
  # Offer timeout (seconds) for the crash prompt; overridable, never inline.
  : "${_CRR_OFFER_TIMEOUT:=30}"
  # [lesson: flag files] A stale flag from a prior action must never act on
  # this launch.
  _crr repair-check --pid "$$" --clear >/dev/null

  # Element-wise flag detection: match whole arguments, never a substring
  # of prompt text (a prompt like "explain -r" is a fresh launch).
  local _arg _resuming=
  for _arg in "$@"; do
    case "$_arg" in
      -r|--resume|--resume=*|-c|--continue|--session-id|--session-id=*)
        _resuming=1; break ;;
    esac
  done
  # The conversation the repair loop resumes: injected sid on a fresh
  # launch, explicit sid on a resume, sid of each consumed relaunch flag.
  local _cur_sid=
  if [ -n "$_resuming" ]; then
    # Extract an explicit resume sid if one was given (-r <sid>,
    # --resume <sid|=sid>, --session-id <sid|=sid>); a value that starts with
    # '-' is another flag, not the sid (`claude -r --model x`), so it is left
    # empty and the sid is guessed from the newest transcript instead.
    local _sid= _want=
    for _arg in "$@"; do
      if [ -n "$_want" ]; then
        case "$_arg" in -*) ;; *) _sid="$_arg" ;; esac
        _want=
        continue
      fi
      case "$_arg" in
        -r|--resume|--session-id) _want=1 ;;
        --resume=*) _sid="${_arg#--resume=}" ;;
        --session-id=*) _sid="${_arg#--session-id=}" ;;
      esac
    done
    if [ -n "$_sid" ]; then
      _crr claude-resume --pid "$$" --cwd "$PWD" --session-id "$_sid" >/dev/null
      _cur_sid="$_sid"
    else
      _crr claude-resume --pid "$$" --cwd "$PWD" >/dev/null
    fi
    command claude "$@"
  else
    local _crr_sid
    _crr_sid="$(_crr claude-launch --pid "$$")"
    if [ -n "$_crr_sid" ]; then
      _cur_sid="$_crr_sid"
      command claude --session-id "$_crr_sid" "$@"
    else
      command claude "$@"
    fi
  fi
  local _code=$?

  local _crashes=0 _flagline _kind _fsid _ans
  while :; do
    # Read, then clear: two calls by design (re-arm window accepted).
    _flagline="$(_crr repair-check --pid "$$")"
    _crr repair-check --pid "$$" --clear >/dev/null
    _kind= _fsid=
    [ -n "$_flagline" ] && read -r _kind _fsid <<< "$_flagline"
    # A bare "relaunch" (no sid) fails the -n test and safely falls
    # through to the absent branches below.
    if [ "$_kind" = relaunch ] && [ -n "$_fsid" ]; then
      _cur_sid="$_fsid"
      _crashes=0
      command claude --resume "$_fsid"
      _code=$?
      continue
    fi
    if [ "$_kind" = close ]; then
      _crr claude-exit --pid "$$"
      exit
    fi
    # Unknown kind or no flag: branch on how claude exited.
    [ "$_code" -eq 0 ] && break
    [ "$_crashes" -ge 2 ] && break
    _ans=
    if [ -t 0 ]; then
      printf 'crr: claude exited unexpectedly (%s). Resume this conversation? [Y/n] ' "$_code" >&2
      IFS= read -r -t "$_CRR_OFFER_TIMEOUT" _ans || _ans=
    fi
    case "$_ans" in n|N|no|No|NO) break ;; esac
    _crashes=$((_crashes + 1))
    if [ -n "$_cur_sid" ]; then
      command claude --resume "$_cur_sid"
      _code=$?
    else
      _crr claude-resume --pid "$$" --cwd "$PWD" >/dev/null
      command claude --continue
      _code=$?
    fi
  done
  _crr claude-exit --pid "$$"
}
```

- [ ] **Step 4: Run the bash repair tests, verify they pass**

Run: `.venv/bin/pytest tests/test_shims.py -k "repair and bash" -v`
Expected: PASS, including the timeout test (~1 s).

- [ ] **Step 5: Full suite + layering gate**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: green, `KEPT`.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shims.py crr/shims/crr.bash
git commit -m "feat(shims): bash repair loop — relaunch/close flag branching after claude exits"
```

---

### Task 3: zsh implementation

**Files:**
- Modify: `tests/test_shims.py` (two list literals only)
- Modify: `crr/shims/crr.zsh` (the `claude` function)

**Interfaces:**
- Consumes: all Task-1 helpers unchanged.
- Produces: `_REPAIR_SHELLS = ["fish", "bash", "zsh"]`, `_TIMED_SHELLS = ["bash", "zsh"]`.

**Note:** zsh may not be installed on this machine — then the zsh params skip (exactly like the existing zsh tests) and the *right reason* check in Step 2 is a skip, not a failure. The code ships regardless (parity is a hard requirement); calibration language in the final report must say so honestly. If zsh IS installed, the steps behave like Task 2's.

- [ ] **Step 1: Enable zsh in the repair tests**

```python
_REPAIR_SHELLS = ["fish", "bash", "zsh"]
_TIMED_SHELLS = ["bash", "zsh"]
```

- [ ] **Step 2: Run, verify fail-or-skip**

Run: `.venv/bin/pytest tests/test_shims.py -k "repair and zsh" -v`
Expected: FAIL if zsh installed (no loop yet), SKIP otherwise.

- [ ] **Step 3: Rewrite the `claude` function in `crr/shims/crr.zsh`**

Replace the entire `claude() { … }` block (keep everything above it unchanged) with — identical to bash except the comment header; zsh supports `local`, herestrings, `read -r -t <num>`, and `$fish_pid`-free `$$`:

```zsh
# claude() wrapper: inject + journal a --session-id on fresh launches
# (sid_source=injected); on resume/continue, journal the resumed session
# (guessed/verified sid) so it is revivable, args untouched. After each
# exit the repair loop consumes the relaunch/close flag: kick → silent
# --resume, close → deregister + exit this shell, bare crash → offer
# ([Y/n]; yes/timeout/no-tty resume, ≤2 attempts), clean exit → prompt.
claude() {
  # Offer timeout (seconds) for the crash prompt; overridable, never inline.
  : "${_CRR_OFFER_TIMEOUT:=30}"
  # [lesson: flag files] A stale flag from a prior action must never act on
  # this launch.
  _crr repair-check --pid "$$" --clear >/dev/null

  # Element-wise flag detection: match whole arguments, never a substring
  # of prompt text (a prompt like "explain -r" is a fresh launch).
  local _arg _resuming=
  for _arg in "$@"; do
    case "$_arg" in
      -r|--resume|--resume=*|-c|--continue|--session-id|--session-id=*)
        _resuming=1; break ;;
    esac
  done
  # The conversation the repair loop resumes: injected sid on a fresh
  # launch, explicit sid on a resume, sid of each consumed relaunch flag.
  local _cur_sid=
  if [ -n "$_resuming" ]; then
    # Extract an explicit resume sid if given (-r <sid>, --resume <sid|=sid>,
    # --session-id <sid|=sid>); a '-'-prefixed value is another flag, not the
    # sid, so it is left empty and the sid is guessed from the newest transcript.
    local _sid= _want=
    for _arg in "$@"; do
      if [ -n "$_want" ]; then
        case "$_arg" in -*) ;; *) _sid="$_arg" ;; esac
        _want=
        continue
      fi
      case "$_arg" in
        -r|--resume|--session-id) _want=1 ;;
        --resume=*) _sid="${_arg#--resume=}" ;;
        --session-id=*) _sid="${_arg#--session-id=}" ;;
      esac
    done
    if [ -n "$_sid" ]; then
      _crr claude-resume --pid "$$" --cwd "$PWD" --session-id "$_sid" >/dev/null
      _cur_sid="$_sid"
    else
      _crr claude-resume --pid "$$" --cwd "$PWD" >/dev/null
    fi
    command claude "$@"
  else
    local _crr_sid
    _crr_sid="$(_crr claude-launch --pid "$$")"
    if [ -n "$_crr_sid" ]; then
      _cur_sid="$_crr_sid"
      command claude --session-id "$_crr_sid" "$@"
    else
      command claude "$@"
    fi
  fi
  local _code=$?

  local _crashes=0 _flagline _kind _fsid _ans
  while :; do
    # Read, then clear: two calls by design (re-arm window accepted).
    _flagline="$(_crr repair-check --pid "$$")"
    _crr repair-check --pid "$$" --clear >/dev/null
    _kind= _fsid=
    [ -n "$_flagline" ] && read -r _kind _fsid <<< "$_flagline"
    # A bare "relaunch" (no sid) fails the -n test and safely falls
    # through to the absent branches below.
    if [ "$_kind" = relaunch ] && [ -n "$_fsid" ]; then
      _cur_sid="$_fsid"
      _crashes=0
      command claude --resume "$_fsid"
      _code=$?
      continue
    fi
    if [ "$_kind" = close ]; then
      _crr claude-exit --pid "$$"
      exit
    fi
    # Unknown kind or no flag: branch on how claude exited.
    [ "$_code" -eq 0 ] && break
    [ "$_crashes" -ge 2 ] && break
    _ans=
    if [ -t 0 ]; then
      printf 'crr: claude exited unexpectedly (%s). Resume this conversation? [Y/n] ' "$_code" >&2
      IFS= read -r -t "$_CRR_OFFER_TIMEOUT" _ans || _ans=
    fi
    case "$_ans" in n|N|no|No|NO) break ;; esac
    _crashes=$((_crashes + 1))
    if [ -n "$_cur_sid" ]; then
      command claude --resume "$_cur_sid"
      _code=$?
    else
      _crr claude-resume --pid "$$" --cwd "$PWD" >/dev/null
      command claude --continue
      _code=$?
    fi
  done
  _crr claude-exit --pid "$$"
}
```

- [ ] **Step 4: Run the zsh repair tests**

Run: `.venv/bin/pytest tests/test_shims.py -k "repair and zsh" -v`
Expected: PASS if zsh installed, SKIP otherwise (state which in the task report).

- [ ] **Step 5: Full suite + layering gate**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: green, `KEPT`.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shims.py crr/shims/crr.zsh
git commit -m "feat(shims): zsh repair loop — relaunch/close flag branching after claude exits"
```

---

### Task 4: Isolated live smoke test (MAIN SESSION ONLY — safety rules §0)

**Never delegated to a subagent. Never touches production.** Everything runs under scratch `XDG_STATE_HOME=$SMOKE/state`, scratch `TMUX_TMPDIR=$SMOKE/tmux` (own tmux server/socket), a fake `claude` (`#!/bin/sh` + `exec sleep 1000000`) first on `PATH`, and `fish --no-config`. Production check (`tmux ls` on the default socket lists exactly the baseline `cc-*` sessions; `ss -tlnp | grep 8377` unchanged) runs BEFORE and AFTER; every scratch artifact is torn down (kill the scratch tmux server, `ps -C sleep` to catch strays — never `pkill -f`).

**Files:**
- Create: `docs/superpowers/smoke/2026-07-27-slice2b-live-smoke.md` — the four scenario results + production-intact evidence.

- [ ] **Step 1: Baseline production check** (record output in the report)
- [ ] **Step 2: Build the scratch harness** — `$SMOKE` under the session scratchpad; generate the fish shim with `crr shim fish --crr-bin .venv/bin/crr`; fake `claude` bindir.
- [ ] **Step 3: kick → silent resume** — in a scratch-socket tmux window running `fish --no-config` sourcing the shim, run `claude`; from outside (same scratch `XDG_STATE_HOME`) run `.venv/bin/crr kick <shell_pid>`; verify the fake claude process group died and a new one appeared with `--resume <sid>` in its argv, no prompt in the pane.
- [ ] **Step 4: close → shell exits** — same setup; `.venv/bin/crr close <shell_pid>`; verify the pane/window closed and the journal entry is gone.
- [ ] **Step 5: crash → offer** — same setup; kill the fake claude group directly (`kill -9 -<pgid>`, pid taken from the scratch journal/ps ancestry — never a name-pattern kill); verify the `[Y/n]` offer appears in the pane; send `n`; verify it stops.
- [ ] **Step 6: clean → return** — fake claude variant `exec sleep 2` (exits 0); verify the wrapper returns to the prompt with no offer and no resume.
- [ ] **Step 7: Teardown + post-check** — `tmux -S <scratch socket> kill-server`, remove `$SMOKE`, `ps -C sleep` shows no strays from us, production check identical to Step 1.
- [ ] **Step 8: Write the report + commit**

```bash
git add docs/superpowers/smoke/2026-07-27-slice2b-live-smoke.md
git commit -m "test(smoke): Slice 2b live smoke in isolation — kick/close/crash/clean verified on Linux/WSL fish"
```

---

### Task 5: Final review + merge

- [ ] Full gates on the branch: `.venv/bin/pytest -q` + `.venv/bin/lint-imports`.
- [ ] Whole-branch final review (opus-tier subagent) against the spec's acceptance criteria + the four hard requirements; fix loop until clean.
- [ ] Merge per handoff §2: `git checkout main && git merge --no-ff feat/shim-repair-loop`, re-run gates on main, `git push origin main`, `gh pr create` (auto-marks merged), delete branch.
- [ ] Close GitHub issue #4 (if it exists) with a completion comment; update any task-tracking docs that reference #4.
