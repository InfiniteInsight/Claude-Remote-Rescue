# Smoother Headless Linux — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a headless host (no GUI tab spawner) at a tty, present crr's restored conversations as tmux windows and drop the user in — the terminal-native equivalent of opening them in tabs.

**Architecture:** A pure-core planner (`crr/core/terminal_reopen.py`) decides the tmux commands and whether to `exec tmux attach`, given the restored session names + whether the caller is inside tmux + whether a tty is present. The cli resolves those inputs, runs the plan's tmux commands via the existing `_run_commands`, and performs the attach via an injected `_exec` seam. Two call sites: the #30 restore prompt (`_rescue_check`) and `crr reopen` (`_cmd_reopen`). Desktop/WSL/macOS-with-GUI are untouched.

**Tech Stack:** Python 3.12 stdlib only; pytest; tmux; the crr one-way layering (`crr.cli → crr.adapters → crr.core`).

## Global Constraints

- Zero runtime deps (stdlib only). One-way layering, machine-enforced: `crr.core` must not import `crr.adapters`/`crr.cli`. (Core building a `["tmux", …]` argv is data, not I/O — consistent with the existing `crr.core.reviver.attach_argv`.)
- No new command / config key / contract bump.
- **The terminal-tmux path activates only when the host has no concept of GUI tabs — `tabs_expected` is `False`** (from `_tab_spawner`). WSL/desktop (`tabs_expected == True`) keep the GUI-tab attempt and the existing notice-on-failure.
- **SAFETY — no test may attach real tmux, link/rename/kill a real window, or exec.** The planner is pure (asserts argvs). The `_exec` seam (`os.execvp`) MUST be monkeypatched to a recorder in every test that reaches it — an un-patched exec would replace the pytest process. tmux commands are run through `_run_commands`, monkeypatched/absorbed in tests.
- `link-window` **shares** a window (keeps each conversation in its own tracked `crr-<sid>` session) — never `move-window`.
- Run `.venv/bin/python -m pytest -q` and `.venv/bin/lint-imports` before each commit. Known-flaky whole-suite test: `tests/test_power_consumer.py::test_power_sees_a_real_separate_awake_process_holding` (real subprocesses, 5s timeout) — if it fails on a full run, re-run in isolation; ignore if it then passes.

---

### Task 1: The pure planner (`crr/core/terminal_reopen.py`)

**Files:**
- Create: `crr/core/terminal_reopen.py`
- Test: `tests/test_terminal_reopen.py`

**Interfaces:**
- Consumes: `crr.core.reviver.attach_argv(name) -> list[str]` (returns `["tmux","attach","-t",name]`).
- Produces:
  - `AGGREGATE_NAME = "crr-restored"`
  - `TerminalReopenPlan` — frozen dataclass: `commands: tuple[tuple[str, ...], ...]`, `exec_argv: tuple[str, ...] | None`, `message: str`.
  - `plan_terminal_reopen(sessions, *, in_tmux, has_tty, current_session, aggregate_exists=False) -> TerminalReopenPlan`, where `sessions` is a list of `(tmux_session_name, window_label)` tuples.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_terminal_reopen.py`:

```python
from crr.core import terminal_reopen as tr


def test_empty_sessions_is_a_noop():
    p = tr.plan_terminal_reopen([], in_tmux=False, has_tty=True, current_session=None)
    assert p.commands == () and p.exec_argv is None and p.message == ""


def test_no_tty_returns_a_notice_never_an_exec():
    p = tr.plan_terminal_reopen(
        [("crr-a", "proj")], in_tmux=False, has_tty=False, current_session=None)
    assert p.commands == () and p.exec_argv is None
    assert "tmux attach -t" in p.message and "crr-a" in p.message


def test_in_tmux_links_each_into_the_current_session_no_exec():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha"), ("crr-b", "beta")],
        in_tmux=True, has_tty=True, current_session="work")
    assert p.exec_argv is None
    assert p.commands == (
        ("tmux", "rename-window", "-t", "crr-a:0", "alpha"),
        ("tmux", "link-window", "-s", "crr-a:0", "-t", "work"),
        ("tmux", "rename-window", "-t", "crr-b:0", "beta"),
        ("tmux", "link-window", "-s", "crr-b:0", "-t", "work"),
    )
    assert "Ctrl-b w" in p.message


def test_not_in_tmux_single_attaches_directly_no_aggregate():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha")], in_tmux=False, has_tty=True, current_session=None)
    assert p.commands == ()
    assert p.exec_argv == ("tmux", "attach", "-t", "crr-a")


def test_not_in_tmux_multi_builds_aggregate_then_execs_attach():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha"), ("crr-b", "beta")],
        in_tmux=False, has_tty=True, current_session=None, aggregate_exists=False)
    assert p.commands == (
        ("tmux", "new-session", "-d", "-s", "crr-restored"),
        ("tmux", "rename-window", "-t", "crr-a:0", "alpha"),
        ("tmux", "link-window", "-s", "crr-a:0", "-t", "crr-restored"),
        ("tmux", "rename-window", "-t", "crr-b:0", "beta"),
        ("tmux", "link-window", "-s", "crr-b:0", "-t", "crr-restored"),
        ("tmux", "kill-window", "-t", "crr-restored:0"),
    )
    assert p.exec_argv == ("tmux", "attach", "-t", "crr-restored")


def test_aggregate_is_killed_first_when_it_already_exists():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha"), ("crr-b", "beta")],
        in_tmux=False, has_tty=True, current_session=None, aggregate_exists=True)
    assert p.commands[0] == ("tmux", "kill-session", "-t", "crr-restored")
    assert p.commands[1] == ("tmux", "new-session", "-d", "-s", "crr-restored")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_terminal_reopen.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the planner**

Create `crr/core/terminal_reopen.py`:

```python
"""Pure planner for reopening restored conversations into THIS terminal.

On a headless host (no GUI tab spawner) the terminal-native "tabs" are tmux
windows. Given the restored conversations' tmux session names, whether the
caller is inside tmux, and whether a tty is present, this decides the exact
tmux commands to run and whether to ``exec tmux attach`` — and does NO I/O, so
every branch is a pure function the cli can test without a tmux server.

``link-window`` SHARES a window between sessions (never ``move-window``): each
conversation stays in its own tracked ``crr-<sid>`` session AND shows up in the
aggregate/current session, so nothing is untracked and detaching kills nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from crr.core.reviver import attach_argv

AGGREGATE_NAME = "crr-restored"


@dataclass(frozen=True)
class TerminalReopenPlan:
    commands: tuple[tuple[str, ...], ...]
    exec_argv: tuple[str, ...] | None
    message: str


def plan_terminal_reopen(
    sessions: Sequence[tuple[str, str]],
    *,
    in_tmux: bool,
    has_tty: bool,
    current_session: str | None,
    aggregate_exists: bool = False,
) -> TerminalReopenPlan:
    sessions = list(sessions)
    if not sessions:
        return TerminalReopenPlan((), None, "")
    names = [name for name, _ in sessions]
    if not has_tty:
        joined = ", ".join(names)
        return TerminalReopenPlan(
            (), None,
            f"{len(sessions)} conversation(s) restored — attach with: "
            f"tmux attach -t <name> ({joined})",
        )

    def rename_and_link(dst: str) -> list[tuple[str, ...]]:
        out: list[tuple[str, ...]] = []
        for name, label in sessions:
            # Rename the SOURCE window (shared, so the name shows everywhere it
            # is linked, and the target is unambiguous without result indices).
            out.append(("tmux", "rename-window", "-t", f"{name}:0", label))
            out.append(("tmux", "link-window", "-s", f"{name}:0", "-t", dst))
        return out

    if in_tmux:
        cmds = tuple(rename_and_link(current_session or ""))
        return TerminalReopenPlan(
            cmds, None,
            f"linked {len(sessions)} restored conversation(s) into this tmux "
            "session — Ctrl-b w to list",
        )

    if len(sessions) == 1:
        return TerminalReopenPlan(
            (), tuple(attach_argv(names[0])),
            f"attaching {names[0]} — Ctrl-b d to detach",
        )

    cmds: list[tuple[str, ...]] = []
    if aggregate_exists:
        # Rebuild so the aggregate reflects the current restored set. Safe:
        # its windows survive in their crr-<sid> sessions (a tmux window dies
        # only when its LAST linking session drops it).
        cmds.append(("tmux", "kill-session", "-t", AGGREGATE_NAME))
    cmds.append(("tmux", "new-session", "-d", "-s", AGGREGATE_NAME))
    cmds.extend(rename_and_link(AGGREGATE_NAME))
    # Drop the placeholder shell new-session created at the default base-index
    # (0 for tmux's default). A non-default base-index leaves a harmless spare
    # window; _run_commands swallows the kill-window miss.
    cmds.append(("tmux", "kill-window", "-t", f"{AGGREGATE_NAME}:0"))
    return TerminalReopenPlan(
        tuple(cmds), tuple(attach_argv(AGGREGATE_NAME)),
        f"attaching {len(sessions)} restored conversation(s) in "
        f"'{AGGREGATE_NAME}' — Ctrl-b w to list, Ctrl-b d to detach",
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_terminal_reopen.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/lint-imports`
Expected: PASS, contract KEPT.

- [ ] **Step 6: Commit**

```bash
git add crr/core/terminal_reopen.py tests/test_terminal_reopen.py
git commit -m "feat(core): pure planner for reopening restored conversations into a terminal"
```

---

### Task 2: tmux adapter — read the current session name

**Files:**
- Modify: `crr/adapters/tmux.py` (add `_current_session_cmd` builder + `current_session_name` method)
- Modify: `crr/core/ports.py` (add `current_session_name` to the `TmuxSpawner` Protocol)
- Test: `tests/test_adapters.py`

**Interfaces:**
- Produces: `RealTmux.current_session_name() -> str | None` — the name of the tmux session the calling process is in (`$TMUX` set), or `None` if not in tmux / unreadable.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_adapters.py` (near the other tmux builder tests; `_Result` and `subprocess`/`tmux` are already imported there):

```python
def test_current_session_cmd_asks_tmux_for_the_session_name():
    assert tmux._current_session_cmd() == ["tmux", "display-message", "-p", "#S"]


def test_current_session_name_parses_the_name(monkeypatch):
    monkeypatch.setattr(tmux.subprocess, "run",
                        lambda *a, **k: _Result(0, stdout="work\n"))
    assert tmux.RealTmux(timeout_seconds=5).current_session_name() == "work"


def test_current_session_name_is_none_when_not_in_tmux(monkeypatch):
    # `display-message` outside tmux exits nonzero ("no server"/"no current...").
    monkeypatch.setattr(tmux.subprocess, "run",
                        lambda *a, **k: _Result(1, stderr="no server running\n"))
    assert tmux.RealTmux(timeout_seconds=5).current_session_name() is None


def test_current_session_name_is_none_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=5)
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    assert tmux.RealTmux(timeout_seconds=5).current_session_name() is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_adapters.py -k current_session -q`
Expected: FAIL — `_current_session_cmd`/`current_session_name` undefined.

- [ ] **Step 3: Implement**

In `crr/adapters/tmux.py`, add the builder next to `_kill_session_cmd`:

```python
def _current_session_cmd() -> list[str]:
    return ["tmux", "display-message", "-p", "#S"]
```

and the method on `RealTmux` (after `kill_session`):

```python
    def current_session_name(self) -> str | None:
        """The name of the tmux session this process is in, or None.

        Used only to target ``link-window`` at the caller's current session
        when they run crr from inside tmux. None (not in tmux, or unreadable)
        is not guessed at — the caller falls back to the aggregate path.
        """
        try:
            result = subprocess.run(
                _current_session_cmd(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        name = result.stdout.strip()
        return name or None
```

In `crr/core/ports.py`, add to the `TmuxSpawner` Protocol (near `kill_session`):

```python
    def current_session_name(self) -> str | None:
        """The tmux session the calling process is in, or None if not in tmux
        / undeterminable. Used to target link-window at the current session."""
        ...
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_adapters.py -k current_session -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/lint-imports`
Expected: PASS, contract KEPT.

- [ ] **Step 6: Commit**

```bash
git add crr/adapters/tmux.py crr/core/ports.py tests/test_adapters.py
git commit -m "feat(adapters): tmux.current_session_name for targeting link-window"
```

---

### Task 3: cli wiring for A — the restore prompt drops you into tmux

**Files:**
- Modify: `crr/cli.py` (add `_exec` seam; add `_terminal_reopen` + `_rescue_prompt_yes` helpers; rewrite the headless branch of `_rescue_check`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `terminal_reopen.plan_terminal_reopen`, `terminal_reopen.AGGREGATE_NAME`, `tmux.RealTmux.current_session_name`, existing `_run_commands`, existing `_tab_spawner`.
- Produces: `_terminal_reopen(sessions, config, sd)` (cli helper; may `exec`, replacing the process), `_rescue_prompt_yes(config, n) -> bool`, module-level `_exec = os.execvp`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (the rescue-check helpers `_rescue_check_setup`, `_FakeTmuxRescued`, `_FakeBoot` already exist):

```python
def test_rescue_check_headless_in_tmux_links_windows_no_exec(tmp_path, monkeypatch, capsys):
    # Headless (tabs_expected False) + inside tmux + [Y] -> link each restored
    # session into the current tmux session; never exec.
    _rescue_check_setup(monkeypatch, tmp_path, [
        {"pid": 42, "tmux_session": "crr-a", "cwd": "/home/u/alpha"},
        {"pid": 43, "tmux_session": "crr-b", "cwd": "/home/u/beta"},
    ])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config: (None, False))  # headless
    monkeypatch.setenv("TMUX", "sock,1,0")  # inside tmux
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    ran = []
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli, "_exec", lambda *a: (_ for _ in ()).throw(
        AssertionError("must not exec when inside tmux")))

    class _T(_FakeTmuxRescued):
        def current_session_name(self): return "work"
    monkeypatch.setattr(cli.tmux, "RealTmux", lambda *a, **k: _T())

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert ["tmux", "link-window", "-s", "crr-a:0", "-t", "work"] in ran
    assert ["tmux", "link-window", "-s", "crr-b:0", "-t", "work"] in ran
    assert "Ctrl-b w" in out


def test_rescue_check_headless_not_in_tmux_execs_attach(tmp_path, monkeypatch, capsys):
    # Headless + NOT in tmux + [Y], single restored -> exec `tmux attach`.
    _rescue_check_setup(monkeypatch, tmp_path, [
        {"pid": 42, "tmux_session": "crr-a", "cwd": "/home/u/alpha"},
    ])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config: (None, False))
    monkeypatch.delenv("TMUX", raising=False)  # not in tmux
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)

    execed = []
    monkeypatch.setattr(cli, "_exec", lambda file, argv: execed.append((file, argv)))

    rc = cli.main(["rescue-check"])
    assert rc == 0
    assert execed == [("tmux", ["tmux", "attach", "-t", "crr-a"])]


def test_rescue_check_headless_decline_does_nothing(tmp_path, monkeypatch, capsys):
    _rescue_check_setup(monkeypatch, tmp_path, [
        {"pid": 42, "tmux_session": "crr-a", "cwd": "/home/u/alpha"},
    ])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config: (None, False))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "n\n")
    monkeypatch.setattr(cli, "_exec", lambda *a: (_ for _ in ()).throw(
        AssertionError("decline must not exec")))
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not now" in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py -k "rescue_check_headless" -q`
Expected: FAIL — the headless branch still only prints the notice; `cli._exec` does not exist.

- [ ] **Step 3: Add the `_exec` seam and the helpers**

In `crr/cli.py`, add near the top-level helpers (e.g. just after `_run_commands`):

```python
# Injected seam (like _run_commands): the ONLY place crr replaces its own
# process. Tests monkeypatch cli._exec to a recorder — an un-patched exec here
# would replace the pytest process.
_exec = os.execvp


def _win_label(cwd: str) -> str:
    """A legible tmux window name for a conversation: its cwd basename."""
    return os.path.basename(cwd.rstrip("/")) or cwd


def _terminal_reopen(sessions: list[tuple[str, str]], config: cfg.Config, sd) -> None:
    """Reopen the given (tmux_session, label) conversations into THIS terminal
    on a headless host: link them into the current tmux session, or build and
    attach the aggregate. May replace this process via `exec tmux attach`.

    Runs the plan's tmux commands under the mutation lock, then RELEASES the
    lock before exec — an exec inherits open fds, so execing while holding the
    lock fd would keep the journal mutation lock held for the whole attach.
    """
    has_tty = sys.stdin.isatty() and sys.stdout.isatty()
    in_tmux = bool(os.environ.get("TMUX"))
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    current = tmux_spawner.current_session_name() if in_tmux else None
    live = tmux_spawner.list_sessions() or set()  # None -> set(): aggregate-exists check
    plan = terminal_reopen.plan_terminal_reopen(
        sessions, in_tmux=in_tmux, has_tty=has_tty, current_session=current,
        aggregate_exists=(terminal_reopen.AGGREGATE_NAME in live),
    )
    if plan.commands:
        with mutation_lock(sd):
            _run_commands([list(c) for c in plan.commands], "reopen")
    if plan.message:
        print(plan.message)
    if plan.exec_argv:
        _exec(plan.exec_argv[0], list(plan.exec_argv))  # replaces this process


def _rescue_prompt_yes(config: cfg.Config, n: int) -> bool:
    """Print the restore [Y/n] prompt and read the answer. True = open them.
    Empty line (Enter) -> True; 'n', any other input, timeout, EOF, Ctrl-C ->
    False. Shared by the GUI and headless branches of _rescue_check."""
    print(f"crr: {n} conversation(s) restored after the last reboot. "
          "Open them in terminal tabs? [Y/n] ", end="", flush=True)
    timeout = config.get("rescue_prompt_timeout_seconds")
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        line = sys.stdin.readline() if ready else ""
    except KeyboardInterrupt:
        print()
        return False
    if not line:
        print()
    answer = line.strip().lower() if line else None
    return answer in ("", "y")
```

Add the import near the other `from crr.core import …` lines:

```python
from crr.core import terminal_reopen
```

- [ ] **Step 4: Rewrite the headless branch of `_rescue_check` and refactor the GUI branch to the shared prompt**

Replace the block from `n = len(found)` through the end of `_rescue_check` (the `tab, tabs_expected = _tab_spawner(config)` line, the notice `if`, the inline prompt, and the `if answer in ("", "y")` / else) with:

```python
    n = len(found)
    tab, tabs_expected = _tab_spawner(config)

    if not tabs_expected:
        # Genuinely headless (no GUI tabs on this host); we have a tty (this
        # function is tty-gated up top). Offer the tmux-window path (#headless).
        if not _rescue_prompt_yes(config, n):
            print("not now — 'crr rescued' lists them")
            return 0
        sessions = [(e["tmux_session"], _win_label(e["cwd"])) for e in found]
        _terminal_reopen(sessions, config, sd)  # may exec (replaces this process)
        return 0

    if tab is None or not tab.available():
        # Host HAS a concept of tabs but none is available right now (e.g. a
        # WSL host with a dead interop handler) — keep the honest notice.
        print(f"crr: {n} conversation(s) restored after the last reboot — "
              "'crr rescued' lists them; attach with: tmux attach -t <name>")
        return 0

    if not _rescue_prompt_yes(config, n):
        print("not now — 'crr rescued' lists them")
        return 0
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    flags = FlagStore(sd)
    with mutation_lock(sd):
        for e in found:
            # reopen (NOT detmux): attach a tab AND keep the conversation
            # tracked, so it stays on the dashboard and is rescued again after
            # the next reboot (#30). Same op as the dashboard Reopen.
            res = ops.reopen(
                JournalStore(sd), ArchiveStore(sd), tmux_spawner, controller, flags,
                boot, probe, e["pid"], _now(),
                grace=config.get("close_grace_seconds"),
                remote_control=config.get("remote_control"),
                tab_spawner=tab, tabs_expected=tabs_expected,
            )
            # The shims invoke `crr rescue-check 2>/dev/null`; the user typed Y
            # and must see failures too, so both outcomes go to stdout.
            print(res.message)
    return 0
```

(This preserves the GUI path exactly — same prompt text, same `ops.reopen` loop — now factored through `_rescue_prompt_yes`. `tmux_spawner` is the one resolved earlier in `_rescue_check`.)

- [ ] **Step 5: Run the rescue-check tests**

Run: `.venv/bin/python -m pytest tests/test_cli.py -k rescue -q`
Expected: PASS — the new headless tests plus the existing GUI/marker/tri-state ones (the GUI happy-path test still asserts the same prompt text and `ops.reopen` discriminator).

- [ ] **Step 6: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/lint-imports`
Expected: PASS, contract KEPT.

- [ ] **Step 7: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit -m "feat(cli): restore prompt drops you into tmux on headless (#headless A)"
```

---

### Task 4: cli wiring for B — a single `crr reopen` drops you in on headless

**Files:**
- Modify: `crr/cli.py` (`_cmd_reopen` — after a successful reopen on a headless host, run the terminal primitive on that one session)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_terminal_reopen`, `_win_label`, `_tab_spawner` (its `tabs_expected`), `JournalStore.read`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_reopen_headless_drops_you_into_tmux(tmp_path, monkeypatch, capsys):
    # On a headless host, `crr reopen <pid>` reopens the parked session AND
    # runs the terminal primitive on it (attach if not in tmux) rather than
    # just printing a message.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)  # available(), list_sessions()
    monkeypatch.setattr(cli, "_tab_spawner", lambda config: (None, False))  # headless
    monkeypatch.delenv("TMUX", raising=False)  # not in tmux
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=42, cwd="/home/u/alpha", host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid), tmux_session="crr-8a1b2c3d"))

    # ops.reopen succeeds (parked entry stays tracked); stub it to avoid real tmux.
    monkeypatch.setattr(cli.ops, "reopen",
                        lambda *a, **k: SimpleNamespace(ok=True, degraded=False,
                                                        message="reopened 42 as crr-8a1b2c3d"))
    execed = []
    monkeypatch.setattr(cli, "_exec", lambda file, argv: execed.append((file, argv)))

    rc = cli.main(["reopen", "42"])
    assert rc == 0
    assert execed == [("tmux", ["tmux", "attach", "-t", "crr-8a1b2c3d"])]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_reopen_headless_drops_you_into_tmux -q`
Expected: FAIL — `_cmd_reopen` prints/returns without execing.

- [ ] **Step 3: Implement in `_cmd_reopen`**

Replace the tail of `_cmd_reopen` (from `print(res.message, …)` through `return 0 if res.ok else 2`) with:

```python
    print(res.message, file=sys.stdout if res.ok else sys.stderr)
    if res.ok and not tabs_expected:
        # Headless host: no GUI tab was possible. Drop the user into the now-
        # parked conversation via tmux (attach, or link into the current
        # session) instead of leaving it merely alive-but-not-in-front-of-them.
        try:
            entry = JournalStore(sd).read(args.pid)
        except (KeyError, contracts.ContractError):
            entry = None
        if entry and entry.get("tmux_session"):
            _terminal_reopen(
                [(entry["tmux_session"], _win_label(entry["cwd"]))], config, sd)
    elif res.degraded:
        # A tabs-capable host where the tab never appeared — the warning is
        # what a human needs (unchanged).
        print("crr reopen: WARNING — no tab opened; the session is running but not in front of you",
              file=sys.stderr)
    return 0 if res.ok else 2
```

(`sd`, `config`, `tabs_expected`, and `args` are all already in scope in `_cmd_reopen`; `contracts` is already imported in cli.)

- [ ] **Step 4: Run the test**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_reopen_headless_drops_you_into_tmux -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/lint-imports`
Expected: PASS, contract KEPT. (Desktop/WSL `crr reopen` tests are unchanged — `tabs_expected` is True there, so the new branch is skipped and the existing degraded-warning path stands.)

- [ ] **Step 6: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit -m "feat(cli): crr reopen drops you into tmux on headless (#headless B)"
```

---

## Self-Review

**Spec coverage:**
- Activation on `not tabs_expected` + tty → Task 3 (rescue-check) & Task 4 (reopen) gate on it; `has_tty` handled in the planner (Task 1) and `_terminal_reopen` (Task 3).
- Shared primitive (in-tmux link / not-in-tmux aggregate / single attach / no-tty notice) → Task 1 planner, executed by `_terminal_reopen` (Task 3).
- rename source window, link-window shares, kill-if-exists rebuild, placeholder drop → Task 1.
- A (rescue prompt now prompts on headless) → Task 3. B (single reopen drops in) → Task 4.
- Pure-core planner + adapter read + injected exec seam + lock-released-before-exec → Tasks 1/2/3.
- No test attaches/execs → every exec-reaching test monkeypatches `cli._exec`; tmux commands go through a monkeypatched `_run_commands`/fake; planner tests are pure.
- No new command/config/contract → confirmed; only a new core module + one adapter method + cli edits.

**Placeholder scan:** none — every step carries real code.

**Type consistency:** `plan_terminal_reopen(sessions, *, in_tmux, has_tty, current_session, aggregate_exists=False)` and `TerminalReopenPlan(commands, exec_argv, message)` are used identically in Task 1 (def/tests) and Task 3 (`_terminal_reopen`). `AGGREGATE_NAME` is referenced from cli. `current_session_name()` (Task 2) matches its call in `_terminal_reopen` (Task 3). `_exec(file, argv)` signature matches `os.execvp` and the test recorders. `_win_label`/`_terminal_reopen` defined in Task 3 are reused in Task 4.
