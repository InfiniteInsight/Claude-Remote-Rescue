# close-flag / 3-state FlagStore — Slice 2a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize the relaunch flag to a 3-state protocol (`relaunch`/`close`/absent), make `ops.close` arm a `close` flag, and add the `crr repair-check` helper the shim will read — the server side of the repair loop, with zero shell code.

**Architecture:** `FlagStore` grows from `arm(pid, sid)` to `arm_relaunch(pid, sid)` / `arm_close(pid)` / `read(pid) → (kind, sid|None)`. `ops.kick` calls `arm_relaunch`; `ops.close` gains a `flags` param and arms `arm_close` before signalling (rolling back if the kill fails). A `crr repair-check --pid [--clear]` CLI helper exposes the flag to the shim. Nothing user-visible changes until Slice 2b's wrapper reads these flags.

**Tech Stack:** Python 3.12 stdlib only, argparse CLI, pytest.

## Global Constraints

- **One-way layering (CI-enforced):** `crr.cli → crr.adapters → crr.core`. Core imports neither adapters nor cli. Run `.venv/bin/lint-imports` — must print `KEPT`.
- **Zero runtime dependencies.** stdlib only.
- **TDD, no exceptions:** every production change is preceded by a failing test watched to fail for the right reason.
- **Classifier-gated, never bare-pid:** `ops.close` still refuses `crashed` sessions (unchanged from Slice 1).
- **Flag armed only when the kill lands:** arm before signalling, roll back (clear) on signal failure — for both kick and close.
- **No page.html change** → no `PAGE_VERSION` bump this slice. **No contract change** → no `SESSIONS/DIAGNOSTICS_CONTRACT_VERSION` bump.
- **Merge process:** local-CI-green = `.venv/bin/pytest -q` + `.venv/bin/lint-imports`. Branch `feat/close-flag-3state` (already created; the spec update is committed there).
- **Context:** Slice 1 shipped `FlagStore.arm(pid, sid)` (1-state), `ops.kick(store, controller, flags, boot, probe, pid, *, grace)` calling `flags.arm(pid, sid)`, and `ops.close(store, controller, boot, probe, pid, *, grace)` with NO flags param. This slice changes those.

---

### Task 1: `FlagStore` → 3-state

**Files:**
- Modify: `crr/core/flags.py` (rewrite the store to 3-state)
- Modify: `crr/core/ops.py` (the one caller: `ops.kick`'s `flags.arm(...)` → `flags.arm_relaunch(...)`)
- Test: `tests/test_flags.py` (rewrite for 3-state), `tests/test_ops.py` (update `FakeFlags` + the kick assertion)

**Interfaces:**
- Produces: module constants `RELAUNCH = "relaunch"`, `CLOSE = "close"`; `FlagStore` methods `arm_relaunch(pid: int, sid: str) -> None`, `arm_close(pid: int) -> None`, `read(pid: int) -> tuple[str, str | None] | None`, `clear(pid: int) -> None`.

- [ ] **Step 1: Rewrite the flag tests for 3-state**

Replace the body of `tests/test_flags.py` with:

```python
from crr.core.flags import FlagStore, RELAUNCH, CLOSE


def test_arm_relaunch_roundtrips_kind_and_sid(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_relaunch(42, "sid-abc")
    assert f.read(42) == (RELAUNCH, "sid-abc")


def test_arm_close_roundtrips_kind_with_no_sid(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_close(7)
    assert f.read(7) == (CLOSE, None)


def test_read_absent_is_none(tmp_path):
    assert FlagStore(tmp_path).read(999) is None


def test_clear_is_idempotent(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_close(7)
    f.clear(7)
    f.clear(7)  # second clear must not raise
    assert f.read(7) is None


def test_arm_overwrites_and_pids_are_isolated(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_relaunch(1, "one")
    f.arm_close(1)              # overwrite pid 1 with a different kind
    f.arm_relaunch(2, "two")
    assert f.read(1) == (CLOSE, None)
    assert f.read(2) == (RELAUNCH, "two")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_flags.py -v`
Expected: FAIL with `ImportError: cannot import name 'RELAUNCH'` (or `AttributeError: ... has no attribute 'arm_relaunch'`).

- [ ] **Step 3: Rewrite `crr/core/flags.py`**

```python
"""Relaunch/close flag store — the shared state between the kick/close ops
and the shim's repair loop.

A flag at ``<state_dir>/relaunch/<shell_pid>`` tells the wrapper what to do
after claude next exits:

- ``relaunch <sid>`` (armed by kick) → silently ``claude --resume <sid>``.
- ``close``           (armed by close) → ``claude-exit`` then ``exit`` the shell.

Absent → the wrapper offers on a crash. Armed only when a kill lands; cleared
by the wrapper at start so a flag never acts on a later launch. Pure core file
I/O, consistent with journal.py (core owns the state-dir filesystem); an
opaque per-pid marker, so it needs no versioned contract.
"""

from __future__ import annotations

import os
from pathlib import Path

RELAUNCH = "relaunch"
CLOSE = "close"


class FlagStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "relaunch"

    def _path(self, pid: int) -> Path:
        return self._dir / str(pid)

    def _write(self, pid: int, content: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._path(pid)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)  # atomic

    def arm_relaunch(self, pid: int, sid: str) -> None:
        self._write(pid, f"{RELAUNCH} {sid}")

    def arm_close(self, pid: int) -> None:
        self._write(pid, CLOSE)

    def read(self, pid: int) -> tuple[str, str | None] | None:
        try:
            content = self._path(pid).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        parts = content.split(None, 1)
        if not parts:
            return None
        sid = parts[1].strip() if len(parts) > 1 else None
        return (parts[0], sid)

    def clear(self, pid: int) -> None:
        self._path(pid).unlink(missing_ok=True)
```

- [ ] **Step 4: Update the one caller in `ops.kick`**

In `crr/core/ops.py`, inside `kick`, change the arm call:

```python
    flags.arm_relaunch(pid, entry["claude"]["session_id"])
```

(It was `flags.arm(pid, entry["claude"]["session_id"])`.)

- [ ] **Step 5: Update `FakeFlags` and the kick assertion in `tests/test_ops.py`**

Replace the `FakeFlags` class with:

```python
class FakeFlags:
    def __init__(self):
        self.armed = {}               # pid -> (kind, sid|None)
    def arm_relaunch(self, pid, sid):
        self.armed[pid] = ("relaunch", sid)
    def arm_close(self, pid):
        self.armed[pid] = ("close", None)
    def clear(self, pid):
        self.armed.pop(pid, None)
```

In `test_kick_arms_the_flag_then_terminates`, change the flag assertion to:

```python
    assert flags.armed[10] == ("relaunch", _SID)
```

- [ ] **Step 6: Run flags + ops tests to verify pass**

Run: `.venv/bin/pytest tests/test_flags.py tests/test_ops.py -v`
Expected: PASS (all flags tests + all existing ops tests, including the updated kick assertion).

- [ ] **Step 7: Full suite + layering + commit**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: all pass; `KEPT`.

```bash
git add crr/core/flags.py crr/core/ops.py tests/test_flags.py tests/test_ops.py
git commit -m "feat(core): FlagStore 3-state (relaunch/close); ops.kick arms arm_relaunch"
```

---

### Task 2: `ops.close` arms the close flag

**Files:**
- Modify: `crr/core/ops.py` (`close` gains a `flags` param and arms `arm_close`)
- Modify: `crr/cli.py` (`_cmd_close` and the web `action_provider` close branch pass `flags`)
- Test: `tests/test_ops.py` (close tests pass `flags`, assert the close flag armed + rolled back)

**Interfaces:**
- Consumes: `FlagStore.arm_close` / `clear` (Task 1).
- Produces: `close(store, controller, flags, boot, probe, pid, *, grace) -> OpResult`.

- [ ] **Step 1: Update the close tests to pass `flags` and assert the close flag**

In `tests/test_ops.py`, **replace the existing three `close` tests** — `test_close_terminates_the_claude_group_of_a_live_session`, `test_close_refuses_a_crashed_session`, and `test_close_reports_when_no_running_claude_group` — with these **four** (each now constructs a `FakeFlags` and passes it; the first also asserts the close flag is armed):

```python
def test_close_terminates_and_arms_the_close_flag(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is True
    assert ctrl.terminated == [(555, 5)]
    assert flags.armed[10] == ("close", None)


def test_close_rolls_the_flag_back_when_the_signal_fails(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    ctrl.raise_on_terminate = True
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert 10 not in flags.armed          # flag survives only if the kill landed


def test_close_refuses_a_crashed_session(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _crashed(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert flags.armed == {}
    assert ctrl.terminated == []


def test_close_reports_when_no_running_claude_group(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[]), FakeFlags()
    res = ops.close(store, ctrl, flags, boot, probe, 10, grace=5)
    assert res.ok is False
    assert flags.armed == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ops.py -k close -v`
Expected: FAIL — `close` currently takes no `flags` param, so `TypeError: close() takes ... positional arguments but ... were given` (or the flag assertion fails).

- [ ] **Step 3: Add the `flags` param + close-flag arming to `ops.close`**

In `crr/core/ops.py`, change `close`'s signature and body:

```python
def close(
    store: JournalStore,
    controller: "ProcessController",
    flags: "FlagStore",
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    *,
    grace: float,
) -> OpResult:
    """End a LIVE/GHOST session (remote `exit`): arm the close flag, then
    SIGTERM the claude group (escalating to SIGKILL after the grace window).
    The wrapper (repair loop) sees the close flag and exits the shell, so the
    terminal closes and the card clears. The flag survives only if the kill
    lands (rolled back on signal failure)."""
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    state = classify(entry, boot, probe)
    if state == CRASHED:
        return OpResult(False, f"session {pid} is crashed, not running — refusing")
    groups = controller.claude_groups(pid)
    if not groups:
        return OpResult(False, f"session {pid}: no running claude process found")
    flags.arm_close(pid)
    try:
        for pgid in groups:
            controller.terminate_group(pgid, grace)
    except OSError as exc:
        flags.clear(pid)  # the kill did not land -> the flag must not linger
        return OpResult(False, f"close {pid} failed to signal: {exc}")
    return OpResult(True, f"closed {pid}")
```

- [ ] **Step 4: Update the CLI + web call sites to pass `flags`**

In `crr/cli.py`:
- `_cmd_close`: it already builds `sd = state_dir.state_dir()`. Add `flags = FlagStore(sd)` (FlagStore is already imported from Task-1 era / Slice 1) and pass it: `ops.close(JournalStore(sd), controller, flags, boot, probe, args.pid, grace=config.get("close_grace_seconds"))`.
- `_cmd_web`'s `action_provider`: the `op == "close"` branch currently calls `ops.close(store, controller, boot, probe, pid, grace=...)`. `flags` is already constructed in `_cmd_web` for the kick branch — pass it: `ops.close(store, controller, flags, boot, probe, pid, grace=config.get("close_grace_seconds"))`.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_ops.py -k close -v`
Expected: PASS (all four close tests).

- [ ] **Step 6: Full suite + layering + commit**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: all pass; `KEPT`.

```bash
git add crr/core/ops.py crr/cli.py tests/test_ops.py
git commit -m "feat(core): ops.close arms the close flag (rollback on failed kill)"
```

---

### Task 3: `crr repair-check` shim helper

**Files:**
- Modify: `crr/cli.py` (`_cmd_repair_check` + parser entry)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `FlagStore.read` / `clear` (Task 1).
- Produces: `crr repair-check --pid <pid> [--clear]` — prints the flag as `relaunch <sid>` / `close` / nothing (absent); `--clear` unlinks it. This is the read/clear surface the Slice-2b shim calls.

- [ ] **Step 1: Write the failing CLI tests**

Add to `tests/test_cli.py`:

```python
def test_repair_check_prints_relaunch_kind_and_sid(tmp_path, monkeypatch, capsys):
    from crr.core.flags import FlagStore
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    FlagStore(tmp_path).arm_relaunch(4242, "sid-xyz")
    rc = cli.main(["repair-check", "--pid", "4242"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "relaunch sid-xyz"


def test_repair_check_prints_close_kind(tmp_path, monkeypatch, capsys):
    from crr.core.flags import FlagStore
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    FlagStore(tmp_path).arm_close(4242)
    rc = cli.main(["repair-check", "--pid", "4242"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "close"


def test_repair_check_absent_prints_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["repair-check", "--pid", "4242"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_repair_check_clear_unlinks_the_flag(tmp_path, monkeypatch, capsys):
    from crr.core.flags import FlagStore
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    flags = FlagStore(tmp_path)
    flags.arm_close(4242)
    rc = cli.main(["repair-check", "--pid", "4242", "--clear"])
    assert rc == 0
    assert flags.read(4242) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_cli.py -k repair_check -v`
Expected: FAIL with `argument command: invalid choice: 'repair-check'`.

- [ ] **Step 3: Implement `_cmd_repair_check` + parser**

In `crr/cli.py` add the handler (near the other shim-facing handlers):

```python
def _cmd_repair_check(args: argparse.Namespace) -> int:
    """[shim] Print the pid's relaunch/close flag for the repair loop, or
    clear it. Output: 'relaunch <sid>' | 'close' | '' (absent)."""
    flags = FlagStore(state_dir.state_dir())
    if args.clear:
        flags.clear(args.pid)
        return 0
    flag = flags.read(args.pid)
    if flag is None:
        return 0
    kind, sid = flag
    print(kind if sid is None else f"{kind} {sid}")
    return 0
```

In `_build_parser`, near the other `[shim]` subparsers (register / claude-exit):

```python
    repair = sub.add_parser("repair-check", help="[shim] read/clear a session's relaunch/close flag")
    repair.add_argument("--pid", type=int, required=True)
    repair.add_argument("--clear", action="store_true")
    repair.set_defaults(func=_cmd_repair_check)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_cli.py -k repair_check -v`
Expected: PASS (all four).

- [ ] **Step 5: Full suite + layering + commit**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: all pass; `KEPT`.

```bash
git add crr/cli.py tests/test_cli.py
git commit -m "feat(cli): crr repair-check — shim reads/clears the relaunch/close flag"
```

---

## Slice 2a Done — Merge

- [ ] Confirm both gates green: `.venv/bin/pytest -q`, `.venv/bin/lint-imports` (`KEPT`). (No node gate needed — page.html unchanged.)
- [ ] Advisor pass, then final whole-branch review, then merge `feat/close-flag-3state` → `main` local-CI-green + push, PR via `gh pr create` (GitHub Actions billing-blocked; same process as #21–#24).
- [ ] Do **not** mark task #4 complete — Slice 2b (the shim repair loop) is the last half. Author its plan next.

## Out of scope for Slice 2a (Slice 2b)

The shell repair loop in `crr.fish`/`crr.bash`/`crr.zsh`: clear stale flag at wrapper start; wrap `command claude` in a loop reading `crr repair-check` after each exit — `relaunch` → silent resume, `close` → `claude-exit` + `exit` the shell, absent+nonzero → offer, absent+0 → return; 2-attempt cap; `test_shims.py` coverage + live Linux/WSL smoke test. Separate plan + branch.
