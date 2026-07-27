# kick / close — Server Side (Slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `crr kick` / `crr close` (CLI + dashboard) that terminate a live claude session by process-group ancestry, with `kick` arming a relaunch flag — the whole server side of task #4, with zero shell code.

**Architecture:** A new read-only-plus-signal port `ProcessController` (ps for ancestry discovery, `os.killpg` for signalling, SIGTERM→grace→SIGKILL) implemented by `PsProcessController`; a core `FlagStore` for relaunch flags under the state dir; `ops.kick`/`ops.close` filling the existing stub in `core/ops.py`, classifier-gated and shared by the CLI handlers and the web `/api/action` endpoint.

**Tech Stack:** Python 3.12 stdlib only (`os.killpg`, `subprocess`, `pathlib`), argparse CLI, stdlib `http.server` dashboard, pytest.

## Global Constraints

- **One-way layering (CI-enforced):** `crr.cli` → `crr.adapters` → `crr.core`. Core imports neither adapters nor cli. Ports (Protocols) live in `crr/core/ports.py`; adapters are selected in `crr.cli`. Run `.venv/bin/lint-imports` — must print `KEPT`.
- **Zero runtime dependencies.** stdlib only.
- **TDD, no exceptions:** every production change is preceded by a failing test you have watched fail for the right reason.
- **Cross-OS parity:** all logic is portable across Linux/WSL/macOS (POSIX `ps -A -o pid=,ppid=,pgid=` + `os.killpg`). No Linux-only branch.
- **Classifier-gated, never bare-pid:** every destructive op refuses `crashed` sessions.
- **Kill by process group of the claude child, never by cmdline.**
- **Failure propagates:** ops return `OpResult(ok, message)`; a false `ok` reaches the CLI exit code and the web 409 (never a swallowed error → green check).
- **`PAGE_VERSION` discipline:** any `crr/core/page.html` change bumps `PAGE_VERSION` in `crr/core/web.py`.
- **Merge process:** local-CI-green = `.venv/bin/pytest -q` + `.venv/bin/lint-imports` + the `node --check` page-JS gate (`pytest -k node`). Feature branch is `feat/kick-close-repair` (already created; the spec is committed there).

---

### Task 1: `ProcessController` port + `PsProcessController` adapter

**Files:**
- Modify: `crr/core/ports.py` (add the `ProcessController` Protocol after `ProcessProbe`)
- Modify: `crr/adapters/process_probe.py` (add pure builders + `PsProcessController`)
- Test: `tests/test_adapters.py` (pure builders) and `tests/test_proc.py` (signal integration)

**Interfaces:**
- Produces:
  - Port `ProcessController` with `claude_groups(shell_pid: int) -> list[int]` and `terminate_group(pgid: int, grace_seconds: float) -> None`.
  - `PsProcessController(timeout_seconds: float)` implementing it.
  - Pure helpers `_ps_snapshot_argv() -> list[str]`, `_parse_ps_rows(stdout: str) -> list[tuple[int,int,int]]`, `_child_groups(rows: list[tuple[int,int,int]], shell_pid: int) -> list[int]`.

- [ ] **Step 1: Write the failing tests for the pure builders**

Add to `tests/test_adapters.py`:

```python
from crr.adapters import process_probe as pp


def test_parse_ps_rows_reads_pid_ppid_pgid():
    out = " 100 1 100\n 200 100 200\n 201 200 200\n"
    assert pp._parse_ps_rows(out) == [(100, 1, 100), (200, 100, 200), (201, 200, 200)]


def test_parse_ps_rows_skips_malformed_lines():
    out = "100 1 100\ngarbage\n\n200 100 200\n"
    assert pp._parse_ps_rows(out) == [(100, 1, 100), (200, 100, 200)]


def test_child_groups_returns_the_claude_group_not_the_shell_group():
    # shell pid 100 in its own group 100; its child 200 leads group 200
    # (claude under job control); 201 is claude's own child, same group 200.
    rows = [(100, 1, 100), (200, 100, 200), (201, 200, 200), (999, 1, 999)]
    assert pp._child_groups(rows, shell_pid=100) == [200]


def test_child_groups_excludes_a_child_that_shares_the_shell_group():
    # Safety: a child in the SHELL's own group is never returned — signalling
    # it would kill the shell. Job-control-off is treated as "nothing to kick".
    rows = [(100, 1, 100), (200, 100, 100)]
    assert pp._child_groups(rows, shell_pid=100) == []


def test_child_groups_empty_when_shell_absent_or_childless():
    assert pp._child_groups([(100, 1, 100)], shell_pid=100) == []
    assert pp._child_groups([(200, 100, 200)], shell_pid=100) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_adapters.py -k "ps_rows or child_groups" -v`
Expected: FAIL with `AttributeError: module 'crr.adapters.process_probe' has no attribute '_parse_ps_rows'`.

- [ ] **Step 3: Add the port Protocol**

In `crr/core/ports.py`, after the `ProcessProbe` class, add:

```python
@runtime_checkable
class ProcessController(Protocol):
    """Signal a live session's claude process group (a mutation — kept
    separate from the read-only ProcessProbe so read callers get no signal
    power). Discovery is by ancestry; signalling targets the whole group."""

    def claude_groups(self, shell_pid: int) -> list[int]:
        """Process-group ids of the shell's non-shell child jobs (claude).

        Excludes the shell's own group, so a returned pgid is always safe to
        signal without killing the shell. Empty when none / shell absent."""
        ...

    def terminate_group(self, pgid: int, grace_seconds: float) -> None:
        """SIGTERM the group, then SIGKILL it if still alive after the grace
        window. Raises OSError if the initial signal cannot be delivered."""
        ...
```

- [ ] **Step 4: Add the pure builders + adapter class**

In `crr/adapters/process_probe.py`, add `import os`, `import signal`, `import time` at the top (keep existing imports), and append:

```python
def _ps_snapshot_argv() -> list[str]:
    # -A all processes; bare `=` headers -> no header line, just the columns.
    return ["ps", "-A", "-o", "pid=,ppid=,pgid="]


def _parse_ps_rows(stdout: str) -> list[tuple[int, int, int]]:
    rows: list[tuple[int, int, int]] = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return rows


def _child_groups(rows: list[tuple[int, int, int]], shell_pid: int) -> list[int]:
    shell_pgid = next((pgid for pid, _ppid, pgid in rows if pid == shell_pid), None)
    if shell_pgid is None:
        return []
    groups: list[int] = []
    for _pid, ppid, pgid in rows:
        if ppid == shell_pid and pgid != shell_pgid and pgid not in groups:
            groups.append(pgid)
    return groups


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours (shouldn't happen for own sessions)


class PsProcessController:
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def claude_groups(self, shell_pid: int) -> list[int]:
        try:
            result = subprocess.run(
                _ps_snapshot_argv(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if result.returncode != 0:
            return []
        return _child_groups(_parse_ps_rows(result.stdout), shell_pid)

    def terminate_group(self, pgid: int, grace_seconds: float) -> None:
        os.killpg(pgid, signal.SIGTERM)  # raises OSError if undeliverable
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return
            time.sleep(0.1)
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # it died in the race between the check and the kill
```

- [ ] **Step 5: Run the pure-builder tests to verify they pass**

Run: `.venv/bin/pytest tests/test_adapters.py -k "ps_rows or child_groups" -v`
Expected: PASS.

- [ ] **Step 6: Write the signal integration test**

Add to `tests/test_proc.py`:

```python
import os
import signal
import subprocess
import time

import pytest

from crr.adapters.process_probe import PsProcessController, _group_alive


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_terminate_group_kills_a_real_process_group():
    # A sleeper in its OWN group (setsid), so terminating the group cannot
    # touch the test runner.
    proc = subprocess.Popen(["sleep", "60"], preexec_fn=os.setsid)
    pgid = os.getpgid(proc.pid)
    try:
        assert _group_alive(pgid) is True
        PsProcessController(2.0).terminate_group(pgid, grace_seconds=0.5)
        proc.wait(timeout=3)
        assert _group_alive(pgid) is False
    finally:
        if proc.poll() is None:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=3)
```

- [ ] **Step 7: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_proc.py -k terminate_group -v`
Expected: PASS (the sleeper dies within the grace window).

- [ ] **Step 8: Layering check + commit**

Run: `.venv/bin/lint-imports` (Expected: `KEPT`)

```bash
git add crr/core/ports.py crr/adapters/process_probe.py tests/test_adapters.py tests/test_proc.py
git commit -m "feat(adapters): ProcessController — ancestry group discovery + SIGTERM/grace/SIGKILL"
```

---

### Task 2: `FlagStore` (relaunch flags)

**Files:**
- Create: `crr/core/flags.py`
- Test: `tests/test_flags.py`

**Interfaces:**
- Produces: `FlagStore(state_dir: Path)` with `arm(pid: int, sid: str) -> None`, `read(pid: int) -> str | None`, `clear(pid: int) -> None`. Flags live at `<state_dir>/relaunch/<pid>`, content = the sid.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_flags.py`:

```python
from crr.core.flags import FlagStore


def test_arm_then_read_roundtrips_the_sid(tmp_path):
    flags = FlagStore(tmp_path)
    flags.arm(42, "sid-abc")
    assert flags.read(42) == "sid-abc"


def test_read_absent_is_none(tmp_path):
    assert FlagStore(tmp_path).read(999) is None


def test_clear_is_idempotent(tmp_path):
    flags = FlagStore(tmp_path)
    flags.arm(7, "s")
    flags.clear(7)
    flags.clear(7)  # second clear must not raise
    assert flags.read(7) is None


def test_arm_overwrites_and_pids_are_isolated(tmp_path):
    flags = FlagStore(tmp_path)
    flags.arm(1, "one")
    flags.arm(1, "one-again")
    flags.arm(2, "two")
    assert flags.read(1) == "one-again"
    assert flags.read(2) == "two"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_flags.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crr.core.flags'`.

- [ ] **Step 3: Implement `FlagStore`**

Create `crr/core/flags.py`:

```python
"""Relaunch-flag store — the one bit of shared state between `ops.kick` and
the shim's repair loop.

A flag at ``<state_dir>/relaunch/<shell_pid>`` (content = the session id to
resume) means "this session was intentionally kicked; resume it silently".
Armed by kick only when the kill lands; cleared by the wrapper at start so a
flag from a session the user later closed on purpose never silently resumes
it. Pure core file I/O, consistent with journal.py (core owns the state-dir
filesystem); a flag is an opaque marker, so it needs no versioned contract.
"""

from __future__ import annotations

import os
from pathlib import Path


class FlagStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "relaunch"

    def _path(self, pid: int) -> Path:
        return self._dir / str(pid)

    def arm(self, pid: int, sid: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._path(pid)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(sid, encoding="utf-8")
        os.replace(tmp, target)  # atomic

    def read(self, pid: int) -> str | None:
        try:
            return self._path(pid).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None

    def clear(self, pid: int) -> None:
        self._path(pid).unlink(missing_ok=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_flags.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Layering check + commit**

Run: `.venv/bin/lint-imports` (Expected: `KEPT`)

```bash
git add crr/core/flags.py tests/test_flags.py
git commit -m "feat(core): FlagStore — relaunch flags for kick/repair-loop"
```

---

### Task 3: `ops.kick` / `ops.close`

**Files:**
- Modify: `crr/core/ops.py` (replace the "kick/close … deliberately not here yet" stub note at line ~11 and add the two functions)
- Test: `tests/test_ops.py`

**Interfaces:**
- Consumes: `ProcessController.claude_groups` / `terminate_group` (Task 1); `FlagStore.arm` / `clear` (Task 2); `classify` + `CRASHED` from `crr.core.classifier`; `OpResult` (already in ops.py).
- Produces:
  - `close(store, controller, boot, probe, pid, now, *, grace) -> OpResult`
  - `kick(store, controller, flags, boot, probe, pid, now, *, grace) -> OpResult`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ops.py`. It already has `_seed(store, pid, *, boot, claude)`, `_claude()`, `FakeBoot(boot)`, `FakeProbe(alive, tty)`, `_NOW`, and imports `JournalStore`. Add two controller/flag fakes and two local helpers built from those, then the cases:

```python
class FakeController:
    def __init__(self, groups):
        self.groups = groups
        self.terminated = []          # (pgid, grace) per call
        self.raise_on_terminate = False

    def claude_groups(self, shell_pid):
        return list(self.groups)

    def terminate_group(self, pgid, grace_seconds):
        if self.raise_on_terminate:
            raise OSError("no such process group")
        self.terminated.append((pgid, grace_seconds))


class FakeFlags:
    def __init__(self):
        self.armed = {}               # pid -> sid
    def arm(self, pid, sid):
        self.armed[pid] = sid
    def clear(self, pid):
        self.armed.pop(pid, None)


def _live(store, pid):
    # same boot + alive + tty  -> classify live
    _seed(store, pid, boot="B", claude=_claude())
    return FakeBoot("B"), FakeProbe(alive=True, tty=True)


def _crashed(store, pid):
    # boot mismatch -> classify crashed (regardless of pid liveness)
    _seed(store, pid, boot="B", claude=_claude())
    return FakeBoot("other"), FakeProbe(alive=True, tty=True)


# --- close ---------------------------------------------------------------

def test_close_terminates_the_claude_group_of_a_live_session(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl = FakeController(groups=[555])
    res = ops.close(store, ctrl, boot, probe, 10, _NOW, grace=5)
    assert res.ok is True
    assert ctrl.terminated == [(555, 5)]


def test_close_refuses_a_crashed_session(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _crashed(store, 10)
    ctrl = FakeController(groups=[555])
    res = ops.close(store, ctrl, boot, probe, 10, _NOW, grace=5)
    assert res.ok is False
    assert ctrl.terminated == []          # never signalled


def test_close_reports_when_no_running_claude_group(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl = FakeController(groups=[])
    res = ops.close(store, ctrl, boot, probe, 10, _NOW, grace=5)
    assert res.ok is False


# --- kick ----------------------------------------------------------------

def test_kick_arms_the_flag_then_terminates(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.kick(store, ctrl, flags, boot, probe, 10, _NOW, grace=5)
    assert res.ok is True
    assert flags.armed[10] == _SID        # sid armed
    assert ctrl.terminated == [(555, 5)]


def test_kick_rolls_the_flag_back_when_the_signal_fails(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _live(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    ctrl.raise_on_terminate = True
    res = ops.kick(store, ctrl, flags, boot, probe, 10, _NOW, grace=5)
    assert res.ok is False
    assert 10 not in flags.armed          # flag survives only if the kill landed


def test_kick_refuses_a_crashed_session(tmp_path):
    store = JournalStore(tmp_path)
    boot, probe = _crashed(store, 10)
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.kick(store, ctrl, flags, boot, probe, 10, _NOW, grace=5)
    assert res.ok is False
    assert flags.armed == {}
    assert ctrl.terminated == []


def test_kick_refuses_a_claude_less_shell(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 11, boot="B", claude=None)   # registered shell, no claude
    ctrl, flags = FakeController(groups=[555]), FakeFlags()
    res = ops.kick(store, ctrl, flags, FakeBoot("B"), FakeProbe(), 11, _NOW, grace=5)
    assert res.ok is False
    assert flags.armed == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ops.py -k "close or kick" -v`
Expected: FAIL with `AttributeError: module 'crr.core.ops' has no attribute 'close'`.

- [ ] **Step 3: Implement `close` and `kick`**

In `crr/core/ops.py`: update the module docstring line that says kick/close are "deliberately not here yet" to note they now live here, add `from crr.core.classifier import CRASHED, LIVE, GHOST, classify` (the file already imports `CRASHED, classify` — extend it), and append:

```python
def close(
    store: JournalStore,
    controller: "ProcessController",
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    now: str,
    *,
    grace: float,
) -> OpResult:
    """End a LIVE/GHOST session (remote `exit`): SIGTERM the claude group,
    escalating to SIGKILL after the grace window. No relaunch."""
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
    try:
        for pgid in groups:
            controller.terminate_group(pgid, grace)
    except OSError as exc:
        return OpResult(False, f"close {pid} failed to signal: {exc}")
    return OpResult(True, f"closed {pid}")


def kick(
    store: JournalStore,
    controller: "ProcessController",
    flags: "FlagStore",
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    now: str,
    *,
    grace: float,
) -> OpResult:
    """Restart claude in place on the same conversation: arm the relaunch
    flag, then SIGTERM/grace/SIGKILL the claude group. The flag survives only
    if the kill lands (rolled back on signal failure), so the shim resumes a
    real kick and never a failed one."""
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    if entry.get("claude") is None:
        return OpResult(False, f"session {pid} has no claude session to relaunch")
    state = classify(entry, boot, probe)
    if state == CRASHED:
        return OpResult(False, f"session {pid} is crashed, not running — use reopen")
    groups = controller.claude_groups(pid)
    if not groups:
        return OpResult(False, f"session {pid}: no running claude process found")
    flags.arm(pid, entry["claude"]["session_id"])
    try:
        for pgid in groups:
            controller.terminate_group(pgid, grace)
    except OSError as exc:
        flags.clear(pid)  # the kill did not land -> the flag must not linger
        return OpResult(False, f"kick {pid} failed to signal: {exc}")
    return OpResult(True, f"kicked {pid} (resuming the same conversation)")
```

Add the type-only imports at the top of `ops.py` under a `TYPE_CHECKING` guard so core stays adapter-free:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from crr.core.ports import ProcessController
    from crr.core.flags import FlagStore
```

(`LIVE`/`GHOST` are imported for symmetry/readability; the gate itself only tests `CRASHED`.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_ops.py -k "close or kick" -v`
Expected: PASS (all seven).

- [ ] **Step 5: Full suite + layering + commit**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: all pass; `KEPT`.

```bash
git add crr/core/ops.py tests/test_ops.py
git commit -m "feat(core): ops.kick/ops.close — classifier-gated group termination + flag arm/rollback"
```

---

### Task 4: CLI commands `crr kick` / `crr close` + config

**Files:**
- Modify: `crr/core/config.py` (add `close_grace_seconds` default)
- Modify: `crr/cli.py` (add `_cmd_kick`, `_cmd_close`, parser entries, imports)
- Test: `tests/test_cli.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `ops.kick`/`ops.close` (Task 3); `PsProcessController` (Task 1); `FlagStore` (Task 2); existing `mutation_lock`, `_now`, `state_dir`, `boot_identity`, `process_probe`, `JournalStore`.

- [ ] **Step 1: Write the failing config test**

Add to `tests/test_config.py`:

```python
def test_close_grace_seconds_default():
    from crr.core.config import Config
    assert Config().get("close_grace_seconds") == 5
```

- [ ] **Step 2: Write the failing CLI tests**

Add to `tests/test_cli.py` (uses the file's existing `state_dir` monkeypatch + seed helpers):

```python
import platform
import pytest


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="boot adapter")
def test_kick_refuses_a_crashed_session(tmp_path, monkeypatch, capsys):
    # A crashed entry is refused BEFORE any signalling (classifier gate),
    # so this exercises the CLI wiring without touching real processes.
    from crr.core.journal import JournalStore
    store = JournalStore(tmp_path)
    store.write(_live_entry(pid=os.getpid(),
                            boot_id="00000000-0000-4000-8000-000000000000"))  # foreign boot -> crashed
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["kick", str(os.getpid())])
    out = capsys.readouterr().out
    assert rc == 1
    assert "crashed" in out


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="boot adapter")
def test_close_reports_no_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["close", "424242"])
    assert rc == 1
    assert "no session" in capsys.readouterr().out
```

> Reuse `_live_entry(...)` — the helper `tests/test_cli.py` already uses for the status tests. If it does not set a claude session, add one to the seeded entry so `kick` reaches the classifier gate rather than the claude-less guard.

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_config.py -k close_grace tests/test_cli.py -k "kick or close" -v`
Expected: FAIL (config key missing / `invalid choice: 'kick'`).

- [ ] **Step 4: Add config default**

In `crr/core/config.py` `DEFAULTS`, next to `zombie_strikes`:

```python
    "close_grace_seconds": 5,        # SIGTERM -> wait -> SIGKILL grace for kick/close
```

- [ ] **Step 5: Implement the CLI handlers + parser**

In `crr/cli.py` add the handlers (near `_cmd_reopen`/`_cmd_remove`):

```python
def _cmd_kick(args: argparse.Namespace) -> int:
    config = _load_config()
    sd = state_dir.state_dir()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr kick: {exc}", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    flags = FlagStore(sd)
    with mutation_lock(sd):
        res = ops.kick(JournalStore(sd), controller, flags, boot, probe,
                       args.pid, _now(), grace=config.get("close_grace_seconds"))
    print(res.message)
    return 0 if res.ok else 1


def _cmd_close(args: argparse.Namespace) -> int:
    config = _load_config()
    sd = state_dir.state_dir()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr close: {exc}", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    with mutation_lock(sd):
        res = ops.close(JournalStore(sd), controller, boot, probe,
                        args.pid, _now(), grace=config.get("close_grace_seconds"))
    print(res.message)
    return 0 if res.ok else 1
```

Add `from crr.core.flags import FlagStore` to the imports. In `_build_parser`, near the `reopen` subparser:

```python
    kick = sub.add_parser("kick", help="restart claude in place on the same conversation")
    kick.add_argument("pid", type=int)
    kick.set_defaults(func=_cmd_kick)

    close = sub.add_parser("close", help="end a live session (remote exit); no revival")
    close.add_argument("pid", type=int)
    close.set_defaults(func=_cmd_close)
```

- [ ] **Step 6: Run to verify pass**

Run: `.venv/bin/pytest tests/test_config.py -k close_grace tests/test_cli.py -k "kick or close" -v`
Expected: PASS.

- [ ] **Step 7: Full suite + layering + commit**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: pass; `KEPT`.

```bash
git add crr/core/config.py crr/cli.py tests/test_cli.py tests/test_config.py
git commit -m "feat(cli): crr kick / crr close commands + close_grace_seconds"
```

---

### Task 5: Web `/api/action` kick/close + dashboard buttons

**Files:**
- Modify: `crr/core/web.py` (extend `ACTIONS`; bump `PAGE_VERSION`)
- Modify: `crr/cli.py` (extend the web `action_provider` with kick/close)
- Modify: `crr/core/page.html` (Kick/Close buttons on live/ghost cards)
- Test: `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `ops.kick`/`ops.close` (Task 3), the web `action_provider` seam already wired for reopen/dismiss/remove.

- [ ] **Step 1: Write the failing web test**

Add to `tests/test_web.py`:

```python
def test_actions_include_kick_and_close():
    from crr.core import web
    assert "kick" in web.ACTIONS
    assert "close" in web.ACTIONS


def test_post_kick_is_accepted_and_dispatched():
    from crr.core import web
    seen = {}
    def action_provider(op, pid):
        seen["op"], seen["pid"] = op, pid
        return True, "kicked 5 (resuming the same conversation)"
    resp = web.handle_request(
        "POST", "/api/action",
        {"Host": "localhost", "Content-Type": "application/json"},
        b'{"op":"kick","pid":5}',
        sessions_provider=lambda: {"contract": 2, "sessions": []},
        action_provider=action_provider,
        allowed_hosts={"localhost"}, allowed_suffixes=(),
    )
    assert resp.status == 200
    assert seen == {"op": "kick", "pid": 5}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_web.py -k "kick or close" -v`
Expected: FAIL (`kick` not in `ACTIONS`; POST returns 400 "invalid op").

- [ ] **Step 3: Extend `ACTIONS` and bump `PAGE_VERSION`**

In `crr/core/web.py`:

```python
ACTIONS = ("reopen", "dismiss", "remove", "kick", "close")
```

```python
PAGE_VERSION = 7  # v7: Kick/Close buttons on live/ghost cards
```

- [ ] **Step 4: Run the web tests to verify pass**

Run: `.venv/bin/pytest tests/test_web.py -k "kick or close" -v`
Expected: PASS.

- [ ] **Step 5: Wire kick/close into the web `action_provider`**

In `crr/cli.py`, inside `make_web_handler`/`_cmd_web`'s `action_provider` (where `reopen`/`dismiss`/`remove` are dispatched under `mutation_lock`), add branches. It must build a controller and (for kick) a FlagStore:

```python
            elif op == "close":
                res = ops.close(store, controller, boot, probe, pid, _now(),
                                grace=config.get("close_grace_seconds"))
            elif op == "kick":
                res = ops.kick(store, controller, flags, boot, probe, pid, _now(),
                               grace=config.get("close_grace_seconds"))
```

Construct `controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))` and `flags = FlagStore(sd)` alongside the other providers in `_cmd_web` (once, captured by the closure), matching how `probe`/`tmux_spawner` are already built there.

- [ ] **Step 6: Add the dashboard buttons**

In `crr/core/page.html`, inside `renderCard`, where the action buttons are added (currently: crashed → Reopen/Dismiss, always → Remove), add live/ghost actions:

```javascript
  if (s.state === "crashed") {
    addBtn("Reopen", "reopen", false);
    addBtn("Dismiss", "dismiss", false);
  } else {                       // live or ghost: a running claude to act on
    addBtn("Kick", "kick", false);
    addBtn("Close", "close", true);
  }
  addBtn("Remove", "remove", true);
```

- [ ] **Step 7: Run the node --check page gate + the web/cli suites**

The dispatch path is already covered (Step 1's `test_post_kick_is_accepted_and_dispatched` proves the web layer routes kick/close to the provider; Task 4's `test_kick_refuses_a_crashed_session` proves the gate). Just re-run the gates the page/JS change touches:

Run: `.venv/bin/pytest -q -k node && .venv/bin/pytest tests/test_web.py tests/test_cli.py -q`
Expected: node gate PASS; suites PASS.

- [ ] **Step 8: Full suite + layering + node gate + commit**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports && .venv/bin/pytest -q -k node`
Expected: all green; `KEPT`.

```bash
git add crr/core/web.py crr/cli.py crr/core/page.html tests/test_web.py tests/test_cli.py
git commit -m "feat(web): kick/close /api/action ops + dashboard buttons (PAGE_VERSION 7)"
```

---

## Slice 1 Done — Merge

- [ ] Confirm all three gates green: `.venv/bin/pytest -q`, `.venv/bin/lint-imports` (`KEPT`), `.venv/bin/pytest -q -k node`.
- [ ] Advisor pass, then merge `feat/kick-close-repair` → `main` local-CI-green and push (GitHub Actions billing-blocked; same process as #21–#23). Open the PR with `gh pr create`.
- [ ] Do **not** mark task #4 complete yet — Slice 2 (the shim repair loop) is the second half. Author Slice 2's plan next.

## Out of scope for Slice 1 (Slice 2)

The shim repair loop (fish/bash/zsh): stale-flag clear at wrapper start, kick-flag → silent resume, nonzero → offer-then-resume, 2-attempt cap, `crr relaunch-flag --check/--clear` helpers. Separate plan + branch, authored after this merges.
