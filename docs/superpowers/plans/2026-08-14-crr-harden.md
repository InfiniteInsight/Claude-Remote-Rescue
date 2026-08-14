# `crr harden` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Windows Update reboot hole that phase 1 could not — and,
because the policy's efficacy is genuinely contested, **measure whether it
held** instead of claiming it works.

**Architecture:** A pure core assesses a registry state against the hours the
user actually works and produces findings plus the exact remediation steps. A
Windows adapter reads that state over unelevated PowerShell. `crr harden`
reports by default and only writes with `--apply`. A separate reader
correlates Windows restart events against the protected window, so crr can
say *"you were restarted at 03:12, outside your active hours, with hardening
applied"* rather than promising it will not happen.

**Tech Stack:** Python 3.11+ stdlib only. `powershell.exe` via WSL interop.
pytest, import-linter.

## Global Constraints

- **Runtime deps stay at zero.** Adding anything to `pyproject.toml`'s
  runtime deps is a design regression (AGENTS.md).
- **One-way layering, machine-enforced:** `crr.cli` → `crr.adapters` →
  `crr.core`. `crr.core` must never import adapters or cli.
- **Test-first.** Every behaviour gets a failing test before implementation.
- **Every judgment-call constant is a named config key** in
  `crr/core/config.py` `DEFAULTS` with a `CONFIG_DEFAULTS_VERSION` bump
  (currently **17**) — never a literal in logic (`tests/test_priors.py`).
- **Null results stay null.** An unknown must never become a positive claim.
  "I could not read the registry" is not "unprotected", and it is certainly
  not "protected".
- **crr must never claim this works.** Microsoft moved these policies under
  "Legacy Policies" on Windows 11 and there are credible reports of them
  being ignored. Every user-facing string says *applied*, never *protected*.
  The measurement is the only thing allowed to speak about efficacy.
- Run before every commit: `.venv/bin/python -m pytest tests/ -q` and
  `.venv/bin/lint-imports`. The pre-commit hook runs both and blocks.
- Spec: `docs/superpowers/specs/2026-08-12-power-block-design.md`
  ("Windows Update").

## HARD RULE — do not modify this machine

**No task in this plan may run `crr harden --apply`, write to `HKLM`, or
change Windows Update settings on this host.** Reads are fine and are how you
verify. Writes are the user's decision, made once, deliberately, with UAC in
front of them.

This is not hypothetical: during an earlier plan a subagent probing
`crr schtasks --install` really created a Windows Scheduled Task on this
machine. It caught and removed it, but do not repeat the shape. Every test
that exercises the apply path injects the command runner. If you believe you
need a real write to verify something, stop and report instead.

## What this host actually looks like (measured 2026-08-13)

Use these as fixtures; do not re-derive them by writing anything.

| | |
|---|---|
| `...\WindowsUpdate\AU` policy key | **absent** — `NoAutoRebootWithLoggedOnUsers` is not set |
| `ActiveHoursStart` / `ActiveHoursEnd` | **7** / **19** |
| `SmartActiveHoursState` | **0** (manual hours in effect) |
| Unelevated `HKLM` reads | work — verified |

The builder works past midnight, so every overnight session currently sits
outside the protected window. That is the hole this plan closes, and it is
plausibly the shape of the Windows-Update reboot in DESIGN.md's provenance.

## File Structure

| File | Responsibility |
|---|---|
| `crr/core/harden.py` (create) | Pure: hour-span arithmetic, assessment, remediation steps |
| `crr/adapters/harden_windows.py` (create) | Read registry state; build (never run) the elevated commands |
| `crr/cli.py` (modify) | `crr harden [--apply]`, doctor lines |
| `crr/core/config.py` (modify) | Active-hours priors + lookback; version 17 → 18 |
| `tests/test_harden.py` (create) | Core tests |
| `tests/test_harden_windows.py` (create) | Adapter + CLI tests |

Out of scope: the Windows tray (its own plan), and anything that writes to
this machine.

---

### Task 1: Active-hours arithmetic

**Files:**
- Create: `crr/core/harden.py`
- Test: `tests/test_harden.py`

**Interfaces:**
- Consumes: nothing
- Produces: `MAX_ACTIVE_HOURS_SPAN = 18`; `span_hours(start: int, end: int) -> int`; `covers(start: int, end: int, hour: int) -> bool`; `valid_span(start: int, end: int) -> str | None`

Active hours may wrap midnight (`start=20, end=8` is a valid 12-hour window),
and Windows caps the span at 18 hours. Both facts are load-bearing and
neither is obvious, so they get their own task.

- [ ] **Step 1: Write the failing test**

Create `tests/test_harden.py`:

```python
"""Windows Update hardening — pure assessment (spec 2026-08-12).

Active hours are the window in which Windows will NOT auto-restart. They
may wrap midnight, and Windows caps the span at 18 hours. Both are easy to
get wrong and neither is visible from the numbers alone.
"""

import pytest

from crr.core.harden import (MAX_ACTIVE_HOURS_SPAN, covers, span_hours,
                             valid_span)


@pytest.mark.parametrize("start,end,expected", [
    (7, 19, 12),      # this host's current setting
    (8, 2, 18),       # wraps midnight, exactly the maximum
    (0, 0, 0),        # degenerate: no window
    (22, 23, 1),
    (23, 1, 2),       # wraps
])
def test_span_handles_midnight_wrap(start, end, expected):
    assert span_hours(start, end) == expected


@pytest.mark.parametrize("start,end,hour,inside", [
    (7, 19, 12, True),
    (7, 19, 3, False),      # the builder's overnight sessions, today
    (8, 2, 1, True),        # wrapped window covers after midnight
    (8, 2, 3, False),
    (8, 2, 23, True),
    (8, 2, 8, True),        # start is inclusive
    (8, 2, 2, False),       # end is exclusive
])
def test_covers_respects_the_wrap_and_the_boundaries(start, end, hour, inside):
    assert covers(start, end, hour) is inside


def test_a_span_over_the_windows_maximum_is_rejected_with_the_reason():
    msg = valid_span(6, 1)   # 19 hours
    assert msg and "18" in msg


def test_a_legal_span_validates():
    assert valid_span(8, 2) is None
    assert MAX_ACTIVE_HOURS_SPAN == 18


@pytest.mark.parametrize("start,end", [(-1, 5), (24, 5), (5, 24), (5, -1)])
def test_hours_outside_0_23_are_rejected(start, end):
    assert valid_span(start, end) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_harden.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crr.core.harden'`

- [ ] **Step 3: Write minimal implementation**

Create `crr/core/harden.py`:

```python
"""Windows Update hardening — the pure half (spec 2026-08-12).

crr cannot stop a forced Update restart from inside WSL. What it can do is
apply the two host policies that are supposed to prevent one, and then
MEASURE whether a restart happened anyway. This module is the assessment;
the adapter reads the registry and the cli decides whether to write.

Nothing here says "protected". Microsoft filed these policies under
"Legacy Policies" on Windows 11 and there are credible reports of them
being ignored, so the honest vocabulary is "applied" plus evidence.
"""

from __future__ import annotations

# Windows refuses an active-hours range longer than this.
MAX_ACTIVE_HOURS_SPAN = 18


def span_hours(start: int, end: int) -> int:
    """Length of the active-hours window, honouring a midnight wrap."""
    return (end - start) % 24


def covers(start: int, end: int, hour: int) -> bool:
    """Is ``hour`` inside the window? Start inclusive, end exclusive."""
    if span_hours(start, end) == 0:
        return False
    return span_hours(start, hour) < span_hours(start, end)


def valid_span(start: int, end: int) -> str | None:
    """None when the range is legal, else why Windows would refuse it."""
    for name, value in (("start", start), ("end", end)):
        if not isinstance(value, int) or not 0 <= value <= 23:
            return f"active hours {name} must be an hour from 0 to 23, got {value!r}"
    span = span_hours(start, end)
    if span > MAX_ACTIVE_HOURS_SPAN:
        return (f"active hours {start}:00-{end}:00 spans {span} hours; "
                f"Windows allows at most {MAX_ACTIVE_HOURS_SPAN}")
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_harden.py -v && .venv/bin/lint-imports`
Expected: 18 passed, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/core/harden.py tests/test_harden.py
git commit -m "feat(core): active-hours arithmetic, midnight wrap and the 18-hour ceiling"
```

---

### Task 2: Config priors

**Files:**
- Modify: `crr/core/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `DEFAULTS["harden_active_hours_start"]` (int), `DEFAULTS["harden_active_hours_end"]` (int), `DEFAULTS["harden_restart_lookback_days"]` (int); `CONFIG_DEFAULTS_VERSION == 18`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_harden_keys_exist_with_a_legal_default_window():
    from crr.core.harden import valid_span
    start = cfg.DEFAULTS["harden_active_hours_start"]
    end = cfg.DEFAULTS["harden_active_hours_end"]
    assert (start, end) == (8, 2)
    # The default must itself be a window Windows would accept — a default
    # that fails validation would make `crr harden` unusable out of the box.
    assert valid_span(start, end) is None
    assert cfg.DEFAULTS["harden_restart_lookback_days"] == 14


def test_config_defaults_version_covers_the_harden_keys():
    assert cfg.CONFIG_DEFAULTS_VERSION >= 18
```

Update the existing exact-version assertion in
`test_vestigial_keys_are_gone_and_version_bumped` from 17 to 18.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -k harden -v`
Expected: FAIL with `KeyError: 'harden_active_hours_start'`

- [ ] **Step 3: Write minimal implementation**

Add a `# v18:` entry at the END of the version-history comment block in
`crr/core/config.py` (never edit an existing entry — read the `v9: SKIPPED`
entry to see why), bump the constant to 18, and append to `DEFAULTS`:

```python
    # Windows Update hardening (spec 2026-08-12). Active hours are the window
    # in which Windows will NOT auto-restart; it may wrap midnight and
    # Windows caps the span at 18 hours (crr.core.harden.MAX_ACTIVE_HOURS_SPAN).
    # 08:00-02:00 is the maximum span anchored on a late working day, chosen
    # because sessions that run past midnight are exactly the ones a forced
    # restart destroys.
    "harden_active_hours_start": 8,
    "harden_active_hours_end": 2,
    # How far back to look for restarts when reporting whether hardening held.
    "harden_restart_lookback_days": 14,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add crr/core/config.py tests/test_config.py
git commit -m "feat(config): harden priors — an 18-hour window that covers past midnight"
```

---

### Task 3: Assess a registry state

**Files:**
- Modify: `crr/core/harden.py`
- Test: `tests/test_harden.py`

**Interfaces:**
- Consumes: Task 1's helpers
- Produces:
  - `HardenState(policy_set: bool | None, active_start: int | None, active_end: int | None, smart_hours: bool | None)` — frozen dataclass; `None` means "could not read"
  - `Finding(key: str, ok: bool | None, detail: str)` — frozen dataclass; `ok=None` is unknown
  - `assess(state: HardenState, want_start: int, want_end: int) -> tuple[Finding, ...]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harden.py`:

```python
from crr.core.harden import Finding, HardenState, assess


def _by_key(findings):
    return {f.key: f for f in findings}


def test_this_hosts_measured_state_reports_both_gaps():
    # Measured 2026-08-13: no AU policy key, hours 7-19, smart hours off.
    state = HardenState(policy_set=False, active_start=7, active_end=19,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["no_auto_reboot"].ok is False
    assert found["active_hours"].ok is False
    # The window is legal, just too narrow — the detail must say WHICH,
    # because "wrong" and "not wide enough" have different fixes.
    assert "7" in found["active_hours"].detail and "19" in found["active_hours"].detail


def test_a_matching_window_and_set_policy_is_clean():
    state = HardenState(policy_set=True, active_start=8, active_end=2,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["no_auto_reboot"].ok is True
    assert found["active_hours"].ok is True


def test_an_unreadable_registry_is_unknown_not_unprotected():
    # The spine rule. "I could not read it" is not "it is not set", and it
    # is certainly not "you are protected".
    state = HardenState(policy_set=None, active_start=None, active_end=None,
                        smart_hours=None)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["no_auto_reboot"].ok is None
    assert found["active_hours"].ok is None
    assert "could not" in found["active_hours"].detail.lower()


def test_smart_active_hours_is_reported_because_it_overrides_the_manual_window():
    # With smart hours ON, Windows picks the window itself and the manual
    # values are not what is in force — reporting them as the truth would
    # be a claim crr cannot make.
    state = HardenState(policy_set=True, active_start=8, active_end=2,
                        smart_hours=True)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["active_hours"].ok is None
    assert "smart" in found["active_hours"].detail.lower()


def test_a_wider_window_than_asked_for_is_not_a_finding():
    # If the user already covers more than crr would set, leave it alone.
    state = HardenState(policy_set=True, active_start=6, active_end=0,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["active_hours"].ok is True


def test_findings_are_frozen():
    import dataclasses
    f = Finding(key="k", ok=True, detail="d")
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.ok = False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_harden.py -k assess -v`
Expected: FAIL with `ImportError: cannot import name 'HardenState'`

- [ ] **Step 3: Write minimal implementation**

Append to `crr/core/harden.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class HardenState:
    """What the host's registry says. ``None`` means it could not be read."""
    policy_set: bool | None
    active_start: int | None
    active_end: int | None
    smart_hours: bool | None


@dataclass(frozen=True)
class Finding:
    key: str
    ok: bool | None        # None = unknown; never coerce it to a bool
    detail: str


def _covers_all_of(outer_start, outer_end, inner_start, inner_end) -> bool:
    """True when the outer window contains the whole inner window."""
    if span_hours(outer_start, outer_end) < span_hours(inner_start, inner_end):
        return False
    return covers(outer_start, outer_end, inner_start) and (
        span_hours(outer_start, inner_end) <= span_hours(outer_start, outer_end)
    )


def assess(state: HardenState, want_start: int, want_end: int) -> tuple[Finding, ...]:
    """Findings for each hardening lever, with unknowns kept unknown."""
    if state.policy_set is None:
        policy = Finding("no_auto_reboot", None,
                         "could not read the Windows Update policy key")
    elif state.policy_set:
        policy = Finding("no_auto_reboot", True,
                         "NoAutoRebootWithLoggedOnUsers is set")
    else:
        policy = Finding("no_auto_reboot", False,
                         "NoAutoRebootWithLoggedOnUsers is not set, so Windows "
                         "may restart to finish an update while you are logged on")

    if state.active_start is None or state.active_end is None:
        hours = Finding("active_hours", None, "could not read active hours")
    elif state.smart_hours:
        hours = Finding("active_hours", None,
                        "smart active hours is on, so Windows chooses the "
                        "window itself and the configured values are not "
                        "what is in force")
    elif _covers_all_of(state.active_start, state.active_end, want_start, want_end):
        hours = Finding("active_hours", True,
                        f"active hours {state.active_start}:00-{state.active_end}:00 "
                        f"already cover {want_start}:00-{want_end}:00")
    else:
        hours = Finding("active_hours", False,
                        f"active hours are {state.active_start}:00-"
                        f"{state.active_end}:00; sessions outside that window "
                        f"are unprotected (crr would set {want_start}:00-"
                        f"{want_end}:00)")
    return (policy, hours)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_harden.py -v && .venv/bin/lint-imports`
Expected: all pass, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/core/harden.py tests/test_harden.py
git commit -m "feat(core): assess the hardening levers, unknowns stay unknown"
```

---

### Task 4: Read the real registry state

**Files:**
- Create: `crr/adapters/harden_windows.py`
- Test: `tests/test_harden_windows.py`

**Interfaces:**
- Consumes: `HardenState` from Task 3
- Produces: `read_command() -> list[str]`; `parse_state(text: str) -> HardenState`; `read_state(timeout: float, run=None) -> HardenState`

Reads only. The elevated writes are Task 6 and are never executed by a test.

- [ ] **Step 1: Write the failing test**

Create `tests/test_harden_windows.py`:

```python
"""Windows Update hardening adapter — reads only.

NOTHING in this file may write to the registry or change Windows Update
settings. The apply path is exercised with an injected runner.
"""

import pytest

from crr.adapters.harden_windows import parse_state, read_command, read_state
from crr.core.harden import HardenState


def test_read_command_is_unelevated_powershell():
    argv = read_command()
    assert argv[0] == "powershell.exe"
    assert "-NoProfile" in argv
    # Reading HKLM does not need elevation; asking for it would put a UAC
    # prompt in front of a status command.
    assert not any("RunAs" in a for a in argv)


def test_parse_this_hosts_measured_state():
    # Measured on the builder's machine 2026-08-13.
    text = "policy=absent\nActiveHoursStart=7\nActiveHoursEnd=19\nSmartActiveHoursState=0\n"
    assert parse_state(text) == HardenState(
        policy_set=False, active_start=7, active_end=19, smart_hours=False)


def test_parse_a_set_policy_and_smart_hours():
    text = "policy=1\nActiveHoursStart=8\nActiveHoursEnd=2\nSmartActiveHoursState=1\n"
    assert parse_state(text) == HardenState(
        policy_set=True, active_start=8, active_end=2, smart_hours=True)


def test_policy_present_but_zero_is_not_set():
    text = "policy=0\nActiveHoursStart=8\nActiveHoursEnd=2\nSmartActiveHoursState=0\n"
    assert parse_state(text).policy_set is False


@pytest.mark.parametrize("text", ["", "garbage", "ActiveHoursStart=notanumber\n"])
def test_unparseable_output_is_unknown_not_a_guess(text):
    state = parse_state(text)
    assert state.active_start is None and state.active_end is None


def test_a_failed_command_is_all_unknown():
    def boom(argv, timeout):
        raise OSError("powershell.exe not found")

    state = read_state(timeout=5, run=boom)
    assert state == HardenState(None, None, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_harden_windows.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `crr/adapters/harden_windows.py`. Read
`crr/adapters/diagnostics_windows.py` first and follow its shape: a pure
parser plus a thin runner, so the parsing is testable without Windows.

The PowerShell must print exactly four `key=value` lines. `policy=absent`
when the AU key does not exist — distinct from `policy=0`, which is the key
present and explicitly disabled. Every read failure yields `None`, never a
default.

Use these registry locations (both verified readable unelevated on this host):
- `HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU` →
  `NoAutoRebootWithLoggedOnUsers`
- `HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings` →
  `ActiveHoursStart`, `ActiveHoursEnd`, `SmartActiveHoursState`

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_harden_windows.py -v && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Verify against this real host — READ ONLY**

Run: `.venv/bin/python -c "from crr.adapters.harden_windows import read_state; print(read_state(timeout=30))"`

Expected, matching the measurement in this plan's header:
`HardenState(policy_set=False, active_start=7, active_end=19, smart_hours=False)`

If it differs, STOP and report — either the adapter is wrong or the host
changed. **Do not write anything to make it match.**

- [ ] **Step 6: Commit**

```bash
git add crr/adapters/harden_windows.py tests/test_harden_windows.py
git commit -m "feat(adapters): read the Windows Update hardening state, unelevated"
```

---

### Task 5: `crr harden` reports, and doctor says the same

**Files:**
- Modify: `crr/cli.py`
- Test: `tests/test_harden_windows.py`

**Interfaces:**
- Consumes: `harden.assess`, `harden_windows.read_state`, `cli._check`
- Produces: `cli._cmd_harden(args) -> int` and a `harden` subparser (no `--apply` yet)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harden_windows.py`:

```python
from crr import cli
from crr.core.harden import HardenState as _HS


def _patch_state(monkeypatch, state):
    monkeypatch.setattr(cli.harden_windows, "read_state", lambda **k: state)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)


def test_harden_reports_the_gaps_and_the_command_that_fixes_them(monkeypatch, capsys):
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out
    assert "NoAutoRebootWithLoggedOnUsers" in out
    assert "crr harden --apply" in out


def test_harden_says_applied_never_protected(monkeypatch, capsys):
    # Microsoft filed these under "Legacy Policies" and they are reported
    # ignored in the wild. crr may claim it applied a setting; it may never
    # claim the machine is safe.
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    cli.main(["harden"])
    out = capsys.readouterr().out.lower()
    assert "protected" not in out
    assert "guarantee" not in out


def test_harden_reports_unknown_rather_than_unprotected(monkeypatch, capsys):
    _patch_state(monkeypatch, _HS(None, None, None, None))
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out.lower()
    assert "unknown" in out or "could not" in out


def test_harden_refuses_on_a_host_with_no_windows_to_harden(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    rc = cli.main(["harden"])
    assert rc != 0
    assert "windows" in capsys.readouterr().err.lower()


def test_doctor_carries_the_same_finding(monkeypatch, capsys):
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: __import__("pathlib").Path("/tmp"))
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "windows update" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_harden_windows.py -k "harden_reports or doctor_carries" -v`
Expected: FAIL — `argument command: invalid choice: 'harden'`

- [ ] **Step 3: Write minimal implementation**

Add the `harden` subparser and `_cmd_harden`, importing `harden_windows` into
`crr/cli.py`'s existing adapter-import block and `harden` into the core block.
Report each `Finding` through the existing `_check(label, ok, detail)` helper
so the output matches doctor's shape, and print the remediation command when
anything is `False`. Add the same block to `_cmd_doctor`.

Refuse on a host that is neither WSL nor Windows, naming the platform —
`boot_identity.detect()` is the house pattern for that shape.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Verify on this real host — READ ONLY**

Run: `.venv/bin/crr harden; echo "rc=$?"` and `.venv/bin/crr doctor | grep -i -A3 "windows update"`

Expected: both report the two gaps measured in this plan's header, rc 0, and
neither uses the word "protected". Report both outputs verbatim.

- [ ] **Step 6: Commit**

```bash
git add crr/cli.py tests/test_harden_windows.py
git commit -m "feat(cli): crr harden reports the Update gaps, and doctor repeats it"
```

---

### Task 6: `--apply`, and the measurement that keeps it honest

**Files:**
- Modify: `crr/adapters/harden_windows.py`, `crr/core/harden.py`, `crr/cli.py`
- Test: `tests/test_harden.py`, `tests/test_harden_windows.py`

**Interfaces:**
- Produces: `harden_windows.apply_commands(want_start, want_end) -> list[list[str]]`;
  `harden.restarts_outside(events, start, end) -> tuple[str, ...]`;
  `crr harden --apply`

**Two halves, and the second is what makes the first honest.** `--apply`
writes the policy. The measurement then reads Windows restart events and
reports any that landed *outside* the active-hours window — evidence the
policy did not hold. Without it, crr would be making a promise the research
says it may not be able to keep.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harden.py`:

```python
from crr.core.harden import restarts_outside


def test_a_restart_inside_the_window_is_not_evidence_of_failure():
    events = ["2026-08-10 14:03:11 [1074] The process ... initiated the restart"]
    assert restarts_outside(events, start=8, end=2) == ()


def test_a_restart_outside_the_window_is_reported():
    # 03:12 with an 08:00-02:00 window: the hardening did not hold.
    events = ["2026-08-11 03:12:45 [6008] The previous system shutdown was unexpected"]
    out = restarts_outside(events, start=8, end=2)
    assert len(out) == 1 and "03:12" in out[0]


def test_an_unparseable_event_line_is_skipped_not_counted_either_way():
    # Cannot tell when it happened -> cannot claim it broke the window, and
    # cannot claim it did not.
    assert restarts_outside(["no timestamp here"], start=8, end=2) == ()
```

Append to `tests/test_harden_windows.py`:

```python
def test_apply_commands_target_both_levers_and_are_elevated():
    cmds = __import__("crr.adapters.harden_windows", fromlist=["x"]).apply_commands(8, 2)
    joined = " ".join(" ".join(c) for c in cmds)
    assert "NoAutoRebootWithLoggedOnUsers" in joined
    assert "ActiveHoursStart" in joined and "ActiveHoursEnd" in joined
    # HKLM writes need elevation; without it the write silently fails and
    # crr would report success for a policy it never set.
    assert "RunAs" in joined


def test_apply_requires_confirmation_and_runs_nothing_when_declined(monkeypatch, capsys):
    ran = []
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    rc = cli.main(["harden", "--apply"])
    assert rc != 0
    assert ran == [], "wrote to the registry without consent"


def test_apply_refuses_without_a_tty_rather_than_writing_unattended(monkeypatch, capsys):
    ran = []
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["harden", "--apply"]) != 0
    assert ran == []


def test_apply_runs_the_commands_once_confirmed(monkeypatch, capsys):
    ran = []
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["harden", "--apply"]) == 0
    assert ran, "confirmed but wrote nothing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_harden.py tests/test_harden_windows.py -k "restart or apply" -v`
Expected: FAIL with `ImportError: cannot import name 'restarts_outside'`

- [ ] **Step 3: Write minimal implementation**

`restarts_outside` parses a leading `YYYY-MM-DD HH:MM:SS` from each event
line (the shape `diagnostics_windows.winevent_command` already produces) and
returns the ones whose hour is not covered by the window. A line without a
parseable timestamp is skipped — it cannot support a claim in either
direction.

`apply_commands` builds elevated `reg add` invocations wrapped in
`powershell.exe Start-Process ... -Verb RunAs`, so Windows shows a UAC prompt.
It **builds** them; only `cli._run_commands` may run them, and only after
confirmation.

`_cmd_harden` gains `--apply`: refuse without a tty (unattended must never be
the path that changes machine policy), prompt otherwise, then run. After a
successful apply, print that the settings were **applied** and that crr will
report any restart that lands outside the window — never that the machine is
now safe.

Wire the measurement into the report: collect events with
`diagnostics_windows.winevent_command`, bounded by
`harden_restart_lookback_days`, and print any `restarts_outside` hits.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Verify on this real host — READ ONLY, DO NOT APPLY**

Run `.venv/bin/crr harden` and confirm it now also reports any restarts from
the last 14 days that fell outside 7:00-19:00. **Do not run `--apply`.**
Report the output verbatim.

- [ ] **Step 6: Commit**

```bash
git add crr/core/harden.py crr/adapters/harden_windows.py crr/cli.py tests/
git commit -m "feat(cli): crr harden --apply, and the measurement that keeps it honest"
```

---

## Self-Review

**Spec coverage.** `NoAutoRebootWithLoggedOnUsers` + widened active hours →
Tasks 3–6. Print by default, `--apply` only with confirmation → Task 6.
"crr never writes to HKLM silently" → Task 6's tty refusal and prompt.
Measurement from boot history → Task 6's `restarts_outside`. Never claiming
efficacy → pinned by `test_harden_says_applied_never_protected`.

**Placeholder scan.** Tasks 1–3 carry complete code. Tasks 4 and 6's Step 3
describe the implementation and point at the sibling adapter rather than
pasting a PowerShell builder, because the emitted script must match the
registry paths verbatim and a copy in the plan would drift; the tests
specify the required output exactly in both cases.

**Type consistency.** `HardenState(policy_set, active_start, active_end,
smart_hours)` and `Finding(key, ok, detail)` are used identically in Tasks
3–6, with `ok`/`policy_set`/`active_*` all tri-state. `assess(state,
want_start, want_end)` matches its callers. `span_hours`/`covers`/
`valid_span` signatures match Tasks 1 and 3.

**The hard rule is repeated in three tasks** (4, 5, 6) rather than only in
the header, because that is where an implementer would be tempted.
