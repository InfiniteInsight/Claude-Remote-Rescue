# Windows Terminal Launcher Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a broken Windows Terminal `wt.exe` App Execution Alias a non-event — CRR falls through to alternate launchers and still opens a visible tab.

**Architecture:** `crr/adapters/tab_spawn_windows.py` gains two alternate argv builders and a tier fallthrough inside `open_tab`. A new pure-core module `crr/core/tab_health.py` stores which tier last worked and formats the `crr doctor` line. `crr/cli.py` records the tier after real spawns and renders the doctor line. The `TabSpawner` port and the other two spawners are untouched.

**Tech Stack:** Python 3.12 stdlib only — `subprocess`, `json`, `pathlib`, `glob`.

## Global Constraints

- Zero runtime dependencies (stdlib only).
- One-way layering: `crr.cli` → `crr.adapters` → `crr.core`. `tab_health.py` lives in `crr.core` and must never import adapters or cli. Decisions in core, I/O in adapters, wiring in cli.
- TDD: tests first, implementation second.
- `TAB_HEALTH_STORE_VERSION = 1` in `crr/core/contracts.py`, added with the store-version cluster (next to `EXCLUSIONS_STORE_VERSION` / `SETTINGS_STORE_VERSION` at lines 99-100).
- No `page.html` change, therefore **no `PAGE_VERSION` bump**. Console output only; the dashboard diagnostics payload and `DIAGNOSTICS_CONTRACT` are untouched.
- **Direct execution of the real `wt.exe` under `C:\Program Files\WindowsApps\...` is BLOCKED** (measured: exit 126, `Permission denied`). Never add a tier that does this.
- `Start-Process` is **fire-and-forget**: Tier 2 and Tier 3 launches are reported as **launched-but-unconfirmed**, never as a confirmed success.
- `TabSpawnTimeout` **must NOT fall through** to the next tier — the tab may have opened, and a second window is worse than waiting (#53).
- Word-form argv only: no shell strings. PowerShell quoting is the adapter's job.
- No test may launch a real window, run a real `wt.exe`/`powershell.exe`, write outside `tmp_path`, or reach an unstubbed `cli._exec` (a `conftest.py` autouse fixture makes that raise).

## Verified launcher matrix (do not re-derive)

Measured 2026-08-29 on WSL Ubuntu-24.04 with `Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe`:

| Route | Result |
|---|---|
| `wt.exe` from PATH (alias stub) | works (exit 0) |
| direct exec of package `wt.exe` | **BLOCKED** — exit 126 `Permission denied` |
| `Start-Process 'shell:appsFolder\Microsoft.WindowsTerminal_8wekyb3d8bbwe!App' -ArgumentList new-tab,…` | works — real WT tab, args pass through |
| `cmd.exe /c start "" wsl.exe -e …` | fails, even from a Windows cwd |
| `Start-Process wsl.exe -ArgumentList '-e',…` | works — plain console window |

The AUMID uses the package **family** name (`Microsoft.WindowsTerminal_8wekyb3d8bbwe`), which is stable across Windows Terminal versions — no version glob to maintain.

## File Structure

- **Create** `crr/core/tab_health.py` — versioned store for the last successful tier + pure doctor-line formatting. One responsibility: "what did tab spawning last do, and how do we say it."
- **Modify** `crr/core/contracts.py` — add `TAB_HEALTH_STORE_VERSION = 1`.
- **Modify** `crr/adapters/tab_spawn_windows.py` — add `AUMID`, `_ps_quote`, `aumid_command`, `console_command`, and tier fallthrough in `open_tab`; expose the tier used via `self.last_tier`.
- **Modify** `crr/cli.py` — record the tier after real spawns; render the doctor line.
- **Create** `tests/test_tab_health.py`; **modify** `tests/test_tab_spawn_windows.py` and `tests/test_cli.py`.

---

### Task 1: Core store and doctor-line formatting

**Files:**
- Create: `crr/core/tab_health.py`
- Create: `tests/test_tab_health.py`
- Modify: `crr/core/contracts.py` (add `TAB_HEALTH_STORE_VERSION = 1` after line 100, `SETTINGS_STORE_VERSION = 1`)

**Interfaces:**
- Consumes: `crr.core.journal.read_json_file`, `crr.core.journal.write_json_atomic`, `crr.core.contracts.store_version_ok`
- Produces (used by Tasks 2-3):
  - `TIER_WT = "wt"`, `TIER_AUMID = "aumid"`, `TIER_CONSOLE = "console"`, `TIER_NONE = "none"`
  - `FILENAME = "tab_health.json"`
  - `TabHealthStore(state_dir: Path)` with `.record(tier: str, detail: str = "", *, now: str, boot_id: str) -> None` and `.read() -> dict | None`
  - `doctor_line(record: dict | None) -> tuple[str, bool | None, str]` returning `(label, ok, detail)` for `cli._check`
  - `ALIAS_NOTE: str`

- [ ] **Step 1: Add the contract constant**

In `crr/core/contracts.py`, immediately after `SETTINGS_STORE_VERSION = 1` (line 100):

```python
TAB_HEALTH_STORE_VERSION = 1
```

- [ ] **Step 2: Write the failing store tests**

Create `tests/test_tab_health.py`:

```python
"""Tab-spawn health store and doctor formatting (spec 2026-08-29)."""

import json

from crr.core import tab_health


def test_read_is_none_when_absent(tmp_path):
    assert tab_health.TabHealthStore(tmp_path).read() is None


def test_record_then_read_round_trip(tmp_path):
    store = tab_health.TabHealthStore(tmp_path)
    store.record(tab_health.TIER_AUMID, "alias stub failed",
                 now="2026-08-29T00:00:00Z", boot_id="b1")
    got = store.read()
    assert got["tier"] == tab_health.TIER_AUMID
    assert got["detail"] == "alias stub failed"
    assert got["ts"] == "2026-08-29T00:00:00Z"
    assert got["boot_id"] == "b1"


def test_corrupt_file_reads_as_none(tmp_path):
    (tmp_path / tab_health.FILENAME).write_text("not json", encoding="utf-8")
    assert tab_health.TabHealthStore(tmp_path).read() is None


def test_non_dict_reads_as_none(tmp_path):
    (tmp_path / tab_health.FILENAME).write_text("[1, 2]", encoding="utf-8")
    assert tab_health.TabHealthStore(tmp_path).read() is None


def test_future_version_reads_as_none(tmp_path):
    (tmp_path / tab_health.FILENAME).write_text(
        json.dumps({"v": 99, "tier": "wt"}), encoding="utf-8")
    assert tab_health.TabHealthStore(tmp_path).read() is None


def test_record_overwrites_the_previous_record(tmp_path):
    store = tab_health.TabHealthStore(tmp_path)
    store.record(tab_health.TIER_WT, "", now="2026-08-29T00:00:00Z", boot_id="b1")
    store.record(tab_health.TIER_CONSOLE, "wt gone",
                 now="2026-08-29T01:00:00Z", boot_id="b1")
    assert store.read()["tier"] == tab_health.TIER_CONSOLE
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tab_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'crr.core.tab_health'`

- [ ] **Step 4: Implement the store**

Create `crr/core/tab_health.py`:

```python
"""Tab-spawn health — which launcher tier last opened a tab (spec 2026-08-29).

Pure core. crr opens a visible tab on WSL through Windows Terminal; when the
``wt.exe`` App Execution Alias is unusable the adapter falls through to
alternate launchers. This module remembers which tier last worked so
``crr doctor`` can say so, and formats that line. It records only outcomes
of spawn attempts that already happened — it never probes, because probing
wt.exe opens a GUI window.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic

FILENAME = "tab_health.json"

# Launcher tiers, best first. Values are persisted — do not rename.
TIER_WT = "wt"            # wt.exe from PATH (the App Execution Alias stub)
TIER_AUMID = "aumid"      # Start-Process shell:appsFolder\...!App (alias bypassed)
TIER_CONSOLE = "console"  # Start-Process wsl.exe (plain window, no Windows Terminal)
TIER_NONE = "none"        # every tier failed


class TabHealthStore:
    """Read/write the last tab-spawn outcome."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / FILENAME

    def read(self) -> dict[str, Any] | None:
        """The last record, or None when absent, corrupt, or a future version."""
        try:
            data = read_json_file(self._path)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if not contracts.store_version_ok(data, contracts.TAB_HEALTH_STORE_VERSION):
            return None
        return data

    def record(self, tier: str, detail: str = "", *, now: str, boot_id: str) -> None:
        write_json_atomic(self._path, {
            "v": contracts.TAB_HEALTH_STORE_VERSION,
            "tier": tier,
            "detail": detail,
            "ts": now,
            "boot_id": boot_id,
        })
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tab_health.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Write the failing doctor-formatting tests**

Append to `tests/test_tab_health.py`:

```python
def test_doctor_line_no_record_is_neutral():
    label, ok, detail = tab_health.doctor_line(None)
    assert ok is True
    assert "not yet exercised" in detail


def test_doctor_line_wt_tier_is_plain_ok():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_WT, "detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is True
    assert "wt.exe" in detail
    # The alias note belongs only to the aumid tier.
    assert "App execution aliases" not in detail


def test_doctor_line_aumid_tier_carries_the_alias_note():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_AUMID, "detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is True
    assert "app package" in detail
    assert "App execution aliases" in detail
    # Never claim the alias IS broken: wt_probe cannot distinguish a disabled
    # alias from a context where wt.exe cannot exec at all.
    assert "alias is broken" not in detail
    assert "alias is disabled" not in detail


def test_doctor_line_console_tier_says_separate_window():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_CONSOLE, "detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is True
    assert "separate window" in detail


def test_doctor_line_none_tier_warns_with_the_error():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_NONE, "detail": "boom",
         "ts": "2026-08-29T00:00:00Z"})
    assert ok is False
    assert "boom" in detail


def test_doctor_line_always_shows_the_timestamp():
    for tier in (tab_health.TIER_WT, tab_health.TIER_AUMID,
                 tab_health.TIER_CONSOLE, tab_health.TIER_NONE):
        _, _, detail = tab_health.doctor_line(
            {"tier": tier, "detail": "", "ts": "2026-08-29T12:34:56Z"})
        assert "2026-08-29T12:34:56Z" in detail, tier


def test_doctor_line_unknown_tier_does_not_crash():
    label, ok, detail = tab_health.doctor_line(
        {"tier": "martian", "detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is None
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tab_health.py -v -k doctor_line`
Expected: FAIL — `AttributeError: module 'crr.core.tab_health' has no attribute 'doctor_line'`

- [ ] **Step 8: Implement the doctor formatting**

Append to `crr/core/tab_health.py`:

```python
LABEL = "tab spawn"

# Shown only for TIER_AUMID. Deliberately does NOT assert the alias is
# broken: wt_probe cannot tell a disabled alias from a context where wt.exe
# cannot exec (tmux, systemd), so a confident claim would sometimes be wrong.
ALIAS_NOTE = (
    "if you want the alias back: Settings -> Apps -> Advanced app settings "
    '-> App execution aliases -> turn on "Terminal (wt.exe)"'
)


def doctor_line(record: dict[str, Any] | None) -> tuple[str, bool | None, str]:
    """Render the tab-spawn health line as ``cli._check(label, ok, detail)`` args.

    ``ok`` is tri-state, matching doctor's renderer: True renders [ok  ],
    False renders [WARN], None renders the unknown state. The timestamp is
    always shown because this reports history, not a live probe — the user
    may have fixed things since.
    """
    if record is None:
        return LABEL, True, "not yet exercised"

    tier = record.get("tier")
    ts = record.get("ts", "unknown time")
    detail = record.get("detail", "")
    when = f"last attempt {ts}"

    if tier == TIER_WT:
        return LABEL, True, f"wt.exe — {when}"
    if tier == TIER_AUMID:
        return LABEL, True, (
            f"via the app package rather than the wt.exe alias; tabs are "
            f"opening normally — {when}. Nothing is broken in crr either "
            f"way; {ALIAS_NOTE}"
        )
    if tier == TIER_CONSOLE:
        return LABEL, True, (
            f"console fallback — Windows Terminal unavailable, tabs open in "
            f"a separate window — {when}"
        )
    if tier == TIER_NONE:
        return LABEL, False, f"no launcher worked: {detail} — {when}"
    return LABEL, None, f"unrecognized tab-spawn record — {when}"
```

- [ ] **Step 9: Run the full test file**

Run: `.venv/bin/pytest tests/test_tab_health.py -v`
Expected: PASS (13 tests)

- [ ] **Step 10: Run the layering contract**

Run: `.venv/bin/lint-imports`
Expected: `Contracts: 1 kept, 0 broken.`

- [ ] **Step 11: Commit**

```bash
git add crr/core/tab_health.py crr/core/contracts.py tests/test_tab_health.py
git commit -m "feat(tab-health): store the last tab-spawn tier and format doctor's line"
```

---

### Task 2: Launcher tiers and fallthrough in the Windows adapter

**Files:**
- Modify: `crr/adapters/tab_spawn_windows.py` (add `AUMID`, `_ps_quote`, `aumid_command`, `console_command`; rewrite `open_tab` at line 136)
- Modify: `tests/test_tab_spawn_windows.py`

**Interfaces:**
- Consumes: `crr.core.tab_health.TIER_WT`, `TIER_AUMID`, `TIER_CONSOLE` (Task 1); existing `wt_command`, `wt_path`, `interop_registered`; `crr.core.ports.TabSpawnTimeout`
- Produces (used by Task 3):
  - `WindowsTerminalSpawner.last_tier: str | None` — the tier that opened the most recent tab, or `None` if none has yet
  - `WindowsTerminalSpawner.last_confirmed: bool` — `True` only for Tier 1 (which exits non-zero on failure); `False` for the fire-and-forget tiers
  - `aumid_command(argv, cwd=None, profile="", distro=None) -> list[str]`
  - `console_command(argv, distro=None) -> list[str]`

**Design note — why `last_tier` and not a return value:** `open_tab` returns `None` in the `TabSpawner` protocol (`crr/core/ports.py:223`) and in all three adapters (`tab_spawn.py:103`, `tab_spawn_linux.py:101`, `tab_spawn_windows.py:136`). Changing the return type would ripple through the port and two unrelated adapters for a Windows-only concern. An attribute read by the caller keeps the blast radius to this file, and Task 3 reads it with `getattr(..., "last_tier", None)` so cli stays agnostic about which spawner it holds.

- [ ] **Step 1: Write the failing argv-builder tests**

Append to `tests/test_tab_spawn_windows.py`:

```python
def test_aumid_command_uses_the_stable_package_family_name():
    cmd = tsw.aumid_command(["tmux", "attach"], distro="Ubuntu-24.04")
    joined = " ".join(cmd)
    assert cmd[0] == "powershell.exe"
    assert "-NoProfile" in cmd
    # Family name, NOT a versioned package full name — stable across upgrades.
    assert "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App" in joined
    assert "1.24" not in joined


def test_aumid_command_passes_new_tab_and_the_wsl_argv():
    cmd = tsw.aumid_command(["tmux", "attach", "-t", "crr-abc"],
                            distro="Ubuntu-24.04")
    joined = " ".join(cmd)
    assert "'new-tab'" in joined
    assert "'wsl.exe'" in joined
    assert "'--distribution','Ubuntu-24.04'" in joined
    assert "'-e','tmux','attach','-t','crr-abc'" in joined


def test_aumid_command_includes_profile_and_cwd_when_given():
    cmd = tsw.aumid_command(["tmux"], cwd="/home/u/p", profile="crr")
    joined = " ".join(cmd)
    assert "'-p','crr'" in joined
    assert "'-d','/home/u/p'" in joined


def test_aumid_command_omits_profile_and_cwd_when_absent():
    joined = " ".join(tsw.aumid_command(["tmux"]))
    assert "'-p'" not in joined
    assert "'-d'" not in joined
    assert "'--distribution'" not in joined


def test_console_command_launches_wsl_without_windows_terminal():
    cmd = tsw.console_command(["tmux", "attach"], distro="Ubuntu-24.04")
    joined = " ".join(cmd)
    assert cmd[0] == "powershell.exe"
    assert "Start-Process wsl.exe" in joined
    assert "'--distribution','Ubuntu-24.04'" in joined
    assert "'-e','tmux','attach'" in joined
    # The console fallback must not reference Windows Terminal at all.
    assert "WindowsTerminal" not in joined
    assert "new-tab" not in joined


def test_ps_quoting_escapes_embedded_single_quotes():
    joined = " ".join(tsw.console_command(["echo", "it's"]))
    # PowerShell escapes a single quote by doubling it.
    assert "'it''s'" in joined


def test_ps_quoting_handles_paths_with_spaces():
    joined = " ".join(tsw.aumid_command(["tmux"], cwd="/home/u/my proj"))
    assert "'/home/u/my proj'" in joined
```

Note: `tsw` is however `tests/test_tab_spawn_windows.py` already imports the module — read the top of that file and match it rather than adding a second import style.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tab_spawn_windows.py -v -k "aumid or console or ps_quoting"`
Expected: FAIL — `AttributeError: module ... has no attribute 'aumid_command'`

- [ ] **Step 3: Implement the argv builders**

In `crr/adapters/tab_spawn_windows.py`, after `wt_command` (which ends at line 104):

```python
# The App User Model ID keyed off the package FAMILY name. Verified
# 2026-08-29: launching through the shell bypasses the wt.exe App Execution
# Alias entirely, and arguments pass through. The family name is stable
# across Windows Terminal versions, so unlike a package path it never goes
# stale on upgrade. Directly executing the real wt.exe under
# C:\Program Files\WindowsApps is NOT an option — measured exit 126,
# Permission denied, because of the WindowsApps ACLs.
AUMID = r"shell:appsFolder\Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"


def _ps_quote(value: str) -> str:
    """Single-quote a value for PowerShell, doubling embedded single quotes."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _arg_list(items: Sequence[str]) -> str:
    """Render a PowerShell -ArgumentList array from word-form argv."""
    return ",".join(_ps_quote(item) for item in items)


def aumid_command(
    argv: Sequence[str],
    cwd: str | None = None,
    profile: str = "",
    distro: str | None = None,
) -> list[str]:
    """Tier 2: open a real Windows Terminal tab without the wt.exe alias."""
    inner: list[str] = ["new-tab"]
    if profile:
        inner += ["-p", profile]
    if cwd:
        inner += ["-d", cwd]
    inner += ["wsl.exe"]
    if distro:
        inner += ["--distribution", distro]
    inner += ["-e", *argv]
    return [
        "powershell.exe", "-NoProfile", "-Command",
        f"Start-Process '{AUMID}' -ArgumentList {_arg_list(inner)}",
    ]


def console_command(argv: Sequence[str], distro: str | None = None) -> list[str]:
    """Tier 3: a plain console window running wsl.exe — no Windows Terminal."""
    inner: list[str] = []
    if distro:
        inner += ["--distribution", distro]
    inner += ["-e", *argv]
    return [
        "powershell.exe", "-NoProfile", "-Command",
        f"Start-Process wsl.exe -ArgumentList {_arg_list(inner)}",
    ]
```

- [ ] **Step 4: Run the builder tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tab_spawn_windows.py -v -k "aumid or console or ps_quoting"`
Expected: PASS

- [ ] **Step 5: Write the failing fallthrough tests**

Append to `tests/test_tab_spawn_windows.py`:

```python
import subprocess

import pytest

from crr.core import tab_health
from crr.core.ports import TabSpawnTimeout


def _spawner():
    return tsw.WindowsTerminalSpawner(timeout_seconds=5, distro="Ubuntu-24.04")


def _runner(fail_first: int):
    """Fake subprocess.run: raise CalledProcessError for the first N calls."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) <= fail_first:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    run.calls = calls
    return run


def test_tier1_success_records_wt_and_stops(monkeypatch):
    runner = _runner(fail_first=0)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    sp.open_tab(["tmux", "attach"])
    assert len(runner.calls) == 1
    assert sp.last_tier == tab_health.TIER_WT
    assert sp.last_confirmed is True


def test_tier1_failure_falls_through_to_aumid(monkeypatch):
    runner = _runner(fail_first=1)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    sp.open_tab(["tmux", "attach"])
    assert len(runner.calls) == 2
    assert "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App" in " ".join(runner.calls[1])
    assert sp.last_tier == tab_health.TIER_AUMID
    # Start-Process is fire-and-forget: launched, not confirmed.
    assert sp.last_confirmed is False


def test_tier2_failure_falls_through_to_console(monkeypatch):
    runner = _runner(fail_first=2)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    sp.open_tab(["tmux", "attach"])
    assert len(runner.calls) == 3
    assert "Start-Process wsl.exe" in " ".join(runner.calls[2])
    assert sp.last_tier == tab_health.TIER_CONSOLE
    assert sp.last_confirmed is False


def test_all_tiers_failing_raises_and_records_none(monkeypatch):
    runner = _runner(fail_first=3)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    with pytest.raises(subprocess.CalledProcessError):
        sp.open_tab(["tmux", "attach"])
    assert len(runner.calls) == 3
    assert sp.last_tier == tab_health.TIER_NONE


def test_a_timeout_does_not_fall_through(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(tsw.subprocess, "run", run)
    sp = _spawner()
    with pytest.raises(TabSpawnTimeout):
        sp.open_tab(["tmux", "attach"])
    # A cold Windows Terminal may still open the tab (#53). Trying the next
    # tier would risk a second window; exactly one attempt must be made.
    assert len(calls) == 1
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tab_spawn_windows.py -v -k "tier or fall_through"`
Expected: FAIL — `AttributeError: 'WindowsTerminalSpawner' object has no attribute 'last_tier'`

- [ ] **Step 7: Implement the fallthrough**

In `crr/adapters/tab_spawn_windows.py`, add to `WindowsTerminalSpawner.__init__` (after line 113, `self._distro = distro`):

```python
        # Which tier opened the most recent tab, and whether that tier can
        # actually prove it. Read by crr.cli after a spawn; see the class
        # docstring for why this is an attribute rather than a return value.
        self.last_tier: str | None = None
        self.last_confirmed: bool = False
```

Then add the import at the top of the file, beside the existing `from crr.core.ports import TabSpawnTimeout`:

```python
from crr.core import tab_health
```

Replace `open_tab` (lines 136-145) entirely with:

```python
    def open_tab(self, argv: Sequence[str], cwd: str | None = None) -> None:
        """Open a visible tab, falling through launcher tiers on failure.

        Tier 1 is wt.exe from PATH (the App Execution Alias). When the alias
        is disabled the stub fails to exec immediately — no hang, no window —
        so falling through costs milliseconds and no UI. Tier 2 reaches the
        same Windows Terminal through the shell AUMID, bypassing the alias.
        Tier 3 drops Windows Terminal entirely for a plain console window.

        A TimeoutExpired never falls through: a cold Windows Terminal can
        outrun the budget and still open the tab (#53), and a second window
        is worse than waiting.
        """
        attempts = (
            (tab_health.TIER_WT,
             wt_command(argv, cwd, self._profile, self._distro), True),
            (tab_health.TIER_AUMID,
             aumid_command(argv, cwd, self._profile, self._distro), False),
            (tab_health.TIER_CONSOLE,
             console_command(argv, self._distro), False),
        )
        last_error: Exception | None = None
        for tier, command, confirmable in attempts:
            try:
                subprocess.run(
                    command, capture_output=True, text=True,
                    timeout=self._timeout, check=True,
                )
            except subprocess.TimeoutExpired as exc:
                self.last_tier = tier
                self.last_confirmed = False
                raise TabSpawnTimeout(exc.timeout or self._timeout) from exc
            except (subprocess.CalledProcessError, OSError) as exc:
                last_error = exc
                continue
            self.last_tier = tier
            # Tiers 2 and 3 use Start-Process, which returns as soon as the
            # process is launched: a zero exit proves the launch, not the tab.
            self.last_confirmed = confirmable
            return
        self.last_tier = tab_health.TIER_NONE
        self.last_confirmed = False
        assert last_error is not None
        raise last_error
```

- [ ] **Step 8: Run the adapter tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tab_spawn_windows.py -v`
Expected: PASS (existing tests plus the new ones)

- [ ] **Step 9: Run the layering contract**

Run: `.venv/bin/lint-imports`
Expected: `Contracts: 1 kept, 0 broken.` (an adapter importing core is the allowed direction)

- [ ] **Step 10: Commit**

```bash
git add crr/adapters/tab_spawn_windows.py tests/test_tab_spawn_windows.py
git commit -m "feat(tab-spawn): fall through to AUMID and console launchers when the wt.exe alias fails"
```

---

### Task 3: CLI wiring — record the tier, render the doctor line

**Files:**
- Modify: `crr/cli.py` (add the doctor check in `_cmd_doctor`, which starts at line ~1576 and renders through `_check(label, ok, detail)` defined at line 1273; construct a `TabHealthStore` and pass it into the `ops` calls below)
- Modify: `crr/core/ops.py` — **the real `open_tab` call sites are here, not in `crr/cli.py`.** cli.py never calls `open_tab`; it only builds a spawner and passes `tab_spawner=...` down into `ops` functions (`reopen`, `detmux`, `untmux`). The actual calls are: `_open_tab` at line ~648 (the helper shared by `reopen` and `_reopen_ghost`), `detmux` at line ~429, and `untmux` at line ~545. Recording must happen at these three sites, which is where success, failure, and `TabSpawnTimeout` are actually distinguished. `tab_health` is core (`crr.core.tab_health`), so `ops.py` importing it is a same-layer, not a layering violation.
- Modify: `tests/test_cli.py`, `tests/test_ops.py`

**Interfaces:**
- Consumes: `crr.core.tab_health.TabHealthStore`, `crr.core.tab_health.doctor_line(record, current_boot_id=None) -> tuple[str, bool | None, str]` (Task 1 — signature updated in the pre-Task-3 fix round to add `current_boot_id`, so a record from before the last reboot is flagged rather than read as live); `spawner.last_tier`, `spawner.last_confirmed` (Task 2); each `ops` function's own existing `now`/`boot: BootIdentity` parameters supply the timestamp and `boot.current()` for the recorder — no new dependency needed there
- Produces: nothing downstream — this is the final task

- [ ] **Step 1: Write the failing doctor-rendering tests**

Append to `tests/test_cli.py`:

```python
def test_doctor_reports_no_tab_spawn_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "tab spawn" in out
    assert "not yet exercised" in out


def test_doctor_reports_the_aumid_tier_with_the_alias_note(tmp_path, monkeypatch, capsys):
    import json
    from crr.core import contracts, tab_health
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / tab_health.FILENAME).write_text(json.dumps({
        "v": contracts.TAB_HEALTH_STORE_VERSION,
        "tier": tab_health.TIER_AUMID, "detail": "",
        "ts": "2026-08-29T12:00:00Z", "boot_id": "b1",
    }), encoding="utf-8")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "app package" in out
    assert "App execution aliases" in out
    assert "2026-08-29T12:00:00Z" in out


def test_doctor_warns_when_no_launcher_worked(tmp_path, monkeypatch, capsys):
    import json
    from crr.core import contracts, tab_health
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / tab_health.FILENAME).write_text(json.dumps({
        "v": contracts.TAB_HEALTH_STORE_VERSION,
        "tier": tab_health.TIER_NONE, "detail": "everything failed",
        "ts": "2026-08-29T12:00:00Z", "boot_id": "b1",
    }), encoding="utf-8")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "everything failed" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -v -k "doctor and tab_spawn or doctor and aumid or doctor and launcher"`
Expected: FAIL — the doctor output contains no "tab spawn" line

- [ ] **Step 3: Add the doctor check**

In `crr/cli.py`, add to the imports beside the other `crr.core` imports:

```python
from crr.core import tab_health
```

`_cmd_doctor` already guards `boot_identity.detect()` in its platform-integration block (around line 1628):

```python
    try:
        adapter = boot_identity.detect()
        _check("boot-identity adapter", True, type(adapter).__name__)
    except NotImplementedError as exc:
        _check("boot-identity adapter", False, str(exc))
```

Initialize `adapter = None` immediately before that `try`, so the name is bound either way. Do **not** call `boot_identity.detect()` a second, unguarded time for the tab-health line — `_cmd_doctor` is the one command whose job is to report problems rather than raise them, and a platform without boot-identity support must not make it crash.

After the existing state-dir check and before the config section, add:

```python
    # Tab-spawn health: which launcher tier last opened a tab. Read from the
    # store, never probed — probing wt.exe opens a GUI window (spec
    # 2026-08-29). current_boot_id flags a record left over from before the
    # last reboot (interop registration, PATH, and the alias state can all
    # change across a reboot); None when boot identity isn't available on
    # this platform, which doctor_line renders exactly as it does today.
    _check(*tab_health.doctor_line(
        tab_health.TabHealthStore(sd).read(),
        current_boot_id=adapter.current() if adapter is not None else None,
    ))
```

Use whatever local name `_cmd_doctor` already binds for the state dir (it is `sd` in the neighbouring commands; read the function and match it).

- [ ] **Step 4: Run the doctor tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli.py -v -k "doctor"`
Expected: PASS

- [ ] **Step 5: Write the failing recording tests, against the real call sites in `ops.py`**

Append to `tests/test_ops.py` (not `test_cli.py` — cli.py never calls `open_tab`; see the Files note above). Use whichever existing fixtures/fakes that file already has for `TabSpawner`, `JournalStore`, `ArchiveStore`, `BootIdentity`, and `ProcessProbe`; match its established style rather than the sketch below:

```python
def test_untmux_records_the_tier_that_opened_the_tab(tmp_path, ...):
    """After untmux opens a tab, the tier it used is persisted."""
    from crr.core import tab_health
    tab_health_store = tab_health.TabHealthStore(tmp_path)

    class _FakeSpawner:
        last_tier = tab_health.TIER_AUMID
        last_confirmed = False
        def open_tab(self, argv, cwd=None) -> None:
            return None

    # ... build store/archive/boot/probe fakes as the file already does ...
    ops.untmux(store, archive, tmux, boot, probe, pid, now,
               tab_spawner=_FakeSpawner(), tab_health=tab_health_store)
    assert tab_health_store.read()["tier"] == tab_health.TIER_AUMID


def test_recording_is_a_no_op_when_no_tab_health_store_is_given(tmp_path, ...):
    """tab_health is optional — every existing ops.py caller/test that omits
    it keeps working exactly as before."""
    # ... call untmux/detmux/reopen without tab_health=... and confirm no
    # crash and no file is written under tmp_path ...
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ops.py -v -k "records_the_tier or no_op_when_no_tab_health"`
Expected: FAIL — `TypeError: untmux() got an unexpected keyword argument 'tab_health'`

- [ ] **Step 7: Implement the recorder helper and thread it through the three call sites**

In `crr/core/ops.py`, near the other small helpers:

```python
def _record_tab_health(tab_health: "TabHealthStore | None", spawner: object,
                        *, now: str, boot_id: str) -> None:
    """Persist which launcher tier the spawner just used, if both a store was
    given and the spawner reports one.

    Only the Windows spawner has tiers; the macOS and Linux spawners have no
    ``last_tier`` and are silently skipped, so this helper is safe to call
    unconditionally after any successful or failed (non-timeout) spawn.
    ``tab_health`` is None for any caller that hasn't wired a store — never
    probes; it only records what a spawn that already happened reported.
    """
    if tab_health is None:
        return
    tier = getattr(spawner, "last_tier", None)
    if tier is None:
        return
    detail = "" if getattr(spawner, "last_confirmed", False) else "launched, unconfirmed"
    tab_health.record(tier, detail, now=now, boot_id=boot_id)
```

Add `from crr.core import tab_health as tab_health_module` (or import `TabHealthStore` directly) beside `ops.py`'s existing `crr.core` imports — this is a core-to-core import, so it does not touch the `cli -> adapters -> core` contract.

Give `reopen`, `_reopen_ghost`, `detmux`, and `untmux` an optional `tab_health: TabHealthStore | None = None` keyword parameter (defaulting to `None` keeps every existing call and test in every one of these four functions valid unchanged). Thread it (along with each function's own existing `now` and `boot`) down to `_open_tab` for the `reopen`/`_reopen_ghost` paths, and call `_record_tab_health` directly in `detmux` and `untmux`.

**`_open_tab` and `untmux` already have a dedicated `except TabSpawnTimeout` clause** (ops.py lines ~661 and ~546) that returns before reaching their success/generic-failure paths — call `_record_tab_health` only in the success branch and in the (non-timeout) `except Exception` branch, never inside the `except TabSpawnTimeout` branch.

**`detmux` does not yet have that split** — its current `except Exception as exc:` at line ~430 catches `TabSpawnTimeout` too (a `TabSpawnTimeout` is-a `Exception`) and treats it identically to a hard failure. Add a dedicated `except TabSpawnTimeout:` clause *before* the existing `except Exception`, keeping `detmux`'s current timeout behavior (return `OpResult(False, ...)`, tab left unrecorded — same as today) unchanged in substance; only the generic `except Exception` branch, run for a genuine non-timeout failure, gets a `_record_tab_health` call. This is required — without it, `detmux`'s timeout path would silently violate the "never record on timeout" rule that `_open_tab` and `untmux` already respect.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ops.py -v -k "records_the_tier or no_op_when_no_tab_health"`
Expected: PASS

- [ ] **Step 9: Wire cli.py to pass a `TabHealthStore` into each `ops` call — and add the timeout-does-not-overwrite test**

In `crr/cli.py`, at each of the three call sites that pass `tab_spawner=...` into `ops.reopen`/`ops.detmux`/`ops.untmux` (search `tab_spawner=`), add `tab_health=tab_health.TabHealthStore(sd)` alongside it, using whatever local name that site already has for the state dir. Do **not** introduce a new spawn or probe; only wire the store through so `ops.py` can record what the existing call already reports.

Append to `tests/test_ops.py`, alongside Step 5's tests:

```python
def test_a_timed_out_spawn_does_not_overwrite_an_existing_record(tmp_path, ...):
    """A TabSpawnTimeout's fate is unknown (#53) — it must not clobber the
    last known-good record with a same-shaped one from an unconfirmed spawn."""
    from crr.core import tab_health
    from crr.core.ports import TabSpawnTimeout
    tab_health_store = tab_health.TabHealthStore(tmp_path)
    tab_health_store.record(tab_health.TIER_WT, "", now="2026-08-29T11:00:00Z",
                             boot_id="b1")

    class _TimeoutSpawner:
        last_tier = tab_health.TIER_WT
        last_confirmed = False
        def open_tab(self, argv, cwd=None) -> None:
            raise TabSpawnTimeout(5)

    # ... build store/archive/boot/probe fakes as the file already does ...
    ops.untmux(store, archive, tmux, boot, probe, pid, now,
               tab_spawner=_TimeoutSpawner(), tab_health=tab_health_store)
    assert tab_health_store.read()["ts"] == "2026-08-29T11:00:00Z"
```

Run: `.venv/bin/pytest tests/test_ops.py -v -k timed_out_spawn_does_not_overwrite`
Expected: FAIL before the `except TabSpawnTimeout` split lands (a broad `except Exception` would call `_record_tab_health` and clobber the prior record); PASS after Step 7.

- [ ] **Step 10: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass. If `test_power_sees_a_real_separate_awake_process_holding` fails, it is a known-flaky cross-process timing test — re-run it alone to confirm, then continue.

- [ ] **Step 11: Run the layering contract**

Run: `.venv/bin/lint-imports`
Expected: `Contracts: 1 kept, 0 broken.`

- [ ] **Step 12: Commit**

```bash
git add crr/cli.py crr/core/ops.py tests/test_cli.py tests/test_ops.py
git commit -m "feat(doctor): record and report which tab-spawn launcher tier is in use"
```

---

## Manual verification before merge

Automated tests cannot exercise a real Windows Terminal — CI is Linux, and no test may open a window. The launcher matrix at the top of this plan is already verified by direct measurement; what remains is confirming the wired-up fallthrough on a real host:

1. On the Windows machine, disable the alias: Settings → Apps → Advanced app settings → App execution aliases → turn **off** "Terminal (wt.exe)".
2. From WSL, trigger a reopen (`crr reopen`) and confirm a Windows Terminal tab still opens — that is Tier 2 working.
3. Run `crr doctor` and confirm the tab-spawn line names the app package and carries the alias note.
4. Re-enable the alias, trigger another reopen, and confirm `crr doctor` returns to the plain `wt.exe` line.

If step 2 fails, Tier 2 is not viable on that host and Tier 3 carries the fallback; record the finding in the spec before merging.

## Self-Review

**Spec coverage:**
- Tiered launcher resolution (T1 wt.exe, T2 AUMID, T3 console) → Task 2 ✓
- Direct package exec excluded → Global Constraints + Task 2 Step 3 comment ✓
- Tiers attempted in-line during a real spawn, never a pre-probe → Task 2 Step 7 ✓
- `TabSpawnTimeout` does not fall through → Task 2 Steps 5 and 7 ✓
- Fire-and-forget launches reported unconfirmed → Task 2 (`last_confirmed`) + Task 3 (`detail`) ✓
- PowerShell quoting for spaces/quotes unit-tested → Task 2 Step 1 ✓
- `wt_probe` / `available(probe=...)` untouched → no task modifies them ✓
- Word-form argv, no shell strings → Task 2 builders return lists ✓
- `tab_health.json` versioned store, degrade-to-None → Task 1 ✓
- `TAB_HEALTH_STORE_VERSION = 1` → Task 1 Step 1 ✓
- Doctor line per tier, timestamp always shown, alias note only on Tier 2, never claims the alias is broken → Task 1 Steps 6-8 ✓
- Pure formatting in core, testable without Windows → Task 1 ✓
- One-way layering → Tasks 1-3, `lint-imports` run in each ✓
- No `PAGE_VERSION` bump → no task touches `page.html` ✓
- Manual host verification → dedicated section ✓

**Placeholder scan:** every code step carries real code, with two kinds of deliberate deferral, both pointing at specific files/symbols rather than an undecided question: the "match the existing local name" instructions (Task 2 Step 1 import style, Task 3 Steps 3 and 9), and Task 3 Steps 5/9's `ops.py` test sketches, which use `...` only for the fake `store`/`archive`/`boot`/`probe` construction that `tests/test_ops.py` already has an established pattern for — the assertions and the calls under test are concrete.

**Type consistency:** `TIER_WT` / `TIER_AUMID` / `TIER_CONSOLE` / `TIER_NONE`, `FILENAME`, `TabHealthStore.record(tier, detail, *, now, boot_id)`, `TabHealthStore.read()`, `doctor_line(record, current_boot_id=None) -> (label, ok, detail)` matching `_check(label, ok, detail)` (signature widened in the pre-Task-3 fix round; the default keeps every Task 1/2 call site valid), `aumid_command(argv, cwd, profile, distro)`, `console_command(argv, distro)`, `last_tier`, `last_confirmed` — all used identically across Tasks 1-3. `ops.reopen` / `ops._reopen_ghost` / `ops.detmux` / `ops.untmux` gain a uniform optional `tab_health: TabHealthStore | None = None`, defaulted so every existing caller and test in `crr/cli.py` and `tests/test_ops.py` stays valid unchanged.
