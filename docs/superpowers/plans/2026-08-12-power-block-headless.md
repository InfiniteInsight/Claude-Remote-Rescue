# Power Blocking (headless hold) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a Claude session is live and the machine is on AC, stop the
computer sleeping on idle and (where the platform allows) refuse a restart —
without ever blocking a lid close.

**Architecture:** A pure core decides *whether* to hold; a `PowerHolder`
adapter per platform performs the hold as a **child process whose lifetime is
the hold**, so it releases when crr dies. A new always-on unit, `crr-awake`,
polls the journal and drives hold/release. Selection branches on
`host.is_wsl()` **before** `platform.system()`, because WSL reports `Linux`
but must use the Windows holder.

**Tech Stack:** Python 3.11+ stdlib only. `systemd-inhibit` (Linux),
`caffeinate` (macOS), `powershell.exe` via WSL interop (Windows). pytest,
import-linter.

## Global Constraints

- **Runtime deps stay at zero.** Adding anything to `pyproject.toml`'s
  runtime deps is a design regression (AGENTS.md).
- **One-way layering, machine-enforced:** `crr.cli` → `crr.adapters` →
  `crr.core`. `crr.core` must never import adapters or cli. Interfaces go in
  `crr/core/ports.py`; adapter selection happens in `crr.cli`.
- **Test-first.** Every behaviour gets a failing test before implementation.
- **Every judgment-call constant is a named config key** in
  `crr/core/config.py` `DEFAULTS` with a `CONFIG_DEFAULTS_VERSION` bump —
  never a literal in logic (`tests/test_priors.py` is the guard).
- **Lid close is never blocked, on any platform.** This is the builder's
  explicit requirement and the single easiest thing to get wrong here.
- **Null results stay null.** An unknown must never become a positive claim.
  `on_ac()` is tri-state; a hold that cannot be verified is reported as
  unverified, not as working.
- Run before every commit: `.venv/bin/python -m pytest tests/ -q` and
  `.venv/bin/lint-imports`. The pre-commit hook runs both and blocks.
- `crr` on PATH is the **deployed** copy. Use `.venv/bin/crr` when testing
  working-tree changes.
- Spec: `docs/superpowers/specs/2026-08-12-power-block-design.md`.

## File Structure

| File | Responsibility |
|---|---|
| `crr/core/power.py` (create) | Pure decision: should we hold, what, and if not why not |
| `crr/core/ports.py` (modify) | `PowerSource` and `PowerHolder` protocols |
| `crr/core/config.py` (modify) | Four new keys + version bump to 16 |
| `crr/adapters/power_source.py` (create) | AC detection: Linux/WSL sysfs, macOS `pmset` |
| `crr/adapters/power_hold_linux.py` (create) | `systemd-inhibit` child + lid-safety check |
| `crr/adapters/power_hold_macos.py` (create) | `caffeinate` child |
| `crr/adapters/power_hold_windows.py` (create) | One `powershell.exe` child holding both locks |
| `crr/cli.py` (modify) | `_power_holder()` selection, `crr power`, doctor lines |
| `tests/test_power.py` (create) | Core decision tests |
| `tests/test_power_adapters.py` (create) | Adapter tests |

Out of scope for this plan (separate plans, same phase): the Windows tray
with its `WM_QUERYENDSESSION` dialog, and `crr harden`.

---

### Task 1: Config keys and version bump

**Files:**
- Modify: `crr/core/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `DEFAULTS["power_block"]` (str), `DEFAULTS["power_block_requires_ac"]` (bool), `DEFAULTS["power_block_max_hours"]` (int), `DEFAULTS["power_poll_seconds"]` (int); `CONFIG_DEFAULTS_VERSION == 16`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_power_block_keys_exist_with_safe_defaults():
    # Off by default: a tool that silently stops your laptop sleeping is a
    # trust hazard, so it must be opted into.
    assert DEFAULTS["power_block"] == "off"
    assert DEFAULTS["power_block_requires_ac"] is True
    assert DEFAULTS["power_block_max_hours"] == 12
    assert DEFAULTS["power_poll_seconds"] == 30


def test_config_defaults_version_covers_the_power_keys():
    assert CONFIG_DEFAULTS_VERSION >= 16
```

Add `CONFIG_DEFAULTS_VERSION` to that file's imports from `crr.core.config`
if it is not already imported.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -k power -v`
Expected: FAIL with `KeyError: 'power_block'`

- [ ] **Step 3: Write minimal implementation**

In `crr/core/config.py`, add above `CONFIG_DEFAULTS_VERSION = 15`:

```python
# v16: added power_block / power_block_requires_ac / power_block_max_hours /
# power_poll_seconds (spec 2026-08-12 — keep the machine up while a session
# is live; see crr.core.power). Off by default: a tool that silently stops a
# laptop sleeping is a trust hazard, so it is opted into, not out of.
```

Change the constant to `CONFIG_DEFAULTS_VERSION = 16`.

Add to the end of `DEFAULTS`, before the closing brace:

```python
    # Power blocking (spec 2026-08-12). "off" | "sleep" | "sleep+shutdown".
    # "sleep" means AUTOMATIC/idle sleep only — lid close is never blocked
    # on any platform, which is a hard requirement, not a default.
    "power_block": "off",
    # A forgotten session must not flatten an unplugged laptop.
    "power_block_requires_ac": True,
    # Belt-and-braces against a holder that outlives crr and blocks
    # restarts with nothing left to explain why.
    "power_block_max_hours": 12,
    "power_poll_seconds": 30,        # how often crr-awake re-decides
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config.py -q && .venv/bin/lint-imports`
Expected: PASS, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/core/config.py tests/test_config.py
git commit -m "feat(config): power-block priors, off by default"
```

---

### Task 2: Core decision — `Decision` and `decide()`

**Files:**
- Create: `crr/core/power.py`
- Test: `tests/test_power.py`

**Interfaces:**
- Consumes: `DEFAULTS` keys from Task 1
- Produces:
  - `Decision(want: frozenset[str], reason: str, withheld: str | None)` — frozen dataclass
  - `decide(live_sessions: int, on_ac: bool | None, mode: str, requires_ac: bool) -> Decision`
  - `MODES: dict[str, frozenset[str]]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_power.py`:

```python
"""Power-block decision (spec 2026-08-12) — pure, no I/O.

The decision is separated from the holding so every reason to NOT hold is
testable without a platform. `withheld` exists because "crr is not holding
anything" is useless to a user without the reason.
"""

from crr.core.power import Decision, decide


def test_off_holds_nothing():
    d = decide(live_sessions=3, on_ac=True, mode="off", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "off" in d.withheld


def test_no_live_session_holds_nothing():
    d = decide(live_sessions=0, on_ac=True, mode="sleep", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "no live" in d.withheld


def test_sleep_mode_holds_only_sleep():
    d = decide(live_sessions=1, on_ac=True, mode="sleep", requires_ac=True)
    assert d.want == frozenset({"sleep"})
    assert d.withheld is None


def test_sleep_plus_shutdown_holds_both():
    d = decide(live_sessions=1, on_ac=True, mode="sleep+shutdown",
               requires_ac=True)
    assert d.want == frozenset({"sleep", "shutdown"})


def test_on_battery_withholds_when_ac_is_required():
    d = decide(live_sessions=2, on_ac=False, mode="sleep", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "battery" in d.withheld


def test_on_battery_holds_when_ac_is_not_required():
    d = decide(live_sessions=2, on_ac=False, mode="sleep", requires_ac=False)
    assert d.want == frozenset({"sleep"})


def test_unknown_power_source_withholds_rather_than_guessing():
    # Spine principle: an unknown must never become a positive claim in
    # EITHER direction. "I could not read the power source" is not "on AC".
    d = decide(live_sessions=2, on_ac=None, mode="sleep", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "cannot tell" in d.withheld


def test_unknown_power_source_is_irrelevant_when_ac_is_not_required():
    d = decide(live_sessions=2, on_ac=None, mode="sleep", requires_ac=False)
    assert d.want == frozenset({"sleep"})


def test_an_unrecognised_mode_holds_nothing_and_says_so():
    # A typo in config.toml must not silently disable protection the user
    # thinks they enabled, nor crash the poll loop.
    d = decide(live_sessions=1, on_ac=True, mode="slep", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "slep" in d.withheld


def test_reason_names_the_session_count_for_the_os_blocking_ui():
    d = decide(live_sessions=3, on_ac=True, mode="sleep", requires_ac=True)
    assert "3" in d.reason and "Claude" in d.reason


def test_decision_is_frozen():
    import dataclasses
    import pytest
    d = decide(live_sessions=1, on_ac=True, mode="sleep", requires_ac=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.want = frozenset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crr.core.power'`

- [ ] **Step 3: Write minimal implementation**

Create `crr/core/power.py`:

```python
"""Should crr hold this machine awake right now? (spec 2026-08-12)

Pure: no I/O, no clock, no platform. The cli owns the probes and the
holder; this module owns the policy, so every reason to withhold is
testable without a laptop to unplug.

``withheld`` is not decoration. "crr is holding nothing" is useless to a
user who enabled the feature and expects protection — the reason is the
whole message, and it is what `crr doctor` prints.
"""

from __future__ import annotations

from dataclasses import dataclass

# What each config mode asks for. "sleep" means AUTOMATIC/idle sleep only:
# lid close is never in scope on any platform (see the spec — logind
# exempts the lid from inhibitors by default, and the Windows/macOS
# mechanisms only ever affected idle).
MODES: dict[str, frozenset[str]] = {
    "off": frozenset(),
    "sleep": frozenset({"sleep"}),
    "sleep+shutdown": frozenset({"sleep", "shutdown"}),
}


@dataclass(frozen=True)
class Decision:
    want: frozenset[str]        # subset of {"sleep", "shutdown"}
    reason: str                 # shown in the OS's own blocking UI
    withheld: str | None = None  # why nothing is held, for doctor


def decide(
    live_sessions: int,
    on_ac: bool | None,
    mode: str,
    requires_ac: bool,
) -> Decision:
    """Decide what to hold, or explain why nothing is held."""
    if mode not in MODES:
        return Decision(frozenset(), "",
                        f"power_block={mode!r} is not a recognised mode "
                        f"({', '.join(sorted(MODES))})")
    want = MODES[mode]
    if not want:
        return Decision(frozenset(), "", "power_block is off")
    if live_sessions <= 0:
        return Decision(frozenset(), "", "no live claude session")
    if requires_ac:
        if on_ac is None:
            # Not "assume AC" and not "assume battery" — either would be a
            # claim nothing measured.
            return Decision(frozenset(), "",
                            "cannot tell whether this machine is on AC")
        if not on_ac:
            return Decision(frozenset(), "",
                            "on battery (power_block_requires_ac is true)")
    plural = "" if live_sessions == 1 else "s"
    return Decision(want, f"crr: {live_sessions} Claude session{plural} live")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power.py -v && .venv/bin/lint-imports`
Expected: 11 passed, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/core/power.py tests/test_power.py
git commit -m "feat(core): pure power-hold decision with an explicit withheld reason"
```

---

### Task 3: `unmet()` — name what the platform cannot do

**Files:**
- Modify: `crr/core/power.py`
- Test: `tests/test_power.py`

**Interfaces:**
- Consumes: `Decision` from Task 2
- Produces: `unmet(capabilities: frozenset[str], want: frozenset[str]) -> tuple[str, ...]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power.py`:

```python
from crr.core.power import unmet


def test_unmet_is_empty_when_the_platform_can_do_it_all():
    assert unmet(frozenset({"sleep", "shutdown"}),
                 frozenset({"sleep", "shutdown"})) == ()


def test_unmet_names_what_this_platform_cannot_deliver():
    # macOS: caffeinate holds sleep, nothing holds shutdown. Doctor must
    # say so rather than silently holding half of what was asked.
    assert unmet(frozenset({"sleep"}),
                 frozenset({"sleep", "shutdown"})) == ("shutdown",)


def test_unmet_is_sorted_so_output_is_stable():
    assert unmet(frozenset(), frozenset({"shutdown", "sleep"})) == (
        "shutdown", "sleep")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power.py -k unmet -v`
Expected: FAIL with `ImportError: cannot import name 'unmet'`

- [ ] **Step 3: Write minimal implementation**

Append to `crr/core/power.py`:

```python
def unmet(capabilities: frozenset[str], want: frozenset[str]) -> tuple[str, ...]:
    """What was asked for that this platform cannot deliver.

    Exists so `crr doctor` can state the gap. Holding half of what was
    requested while reporting success is the failure mode this whole
    design keeps running into: a hold that succeeds loudly and protects
    nothing.
    """
    return tuple(sorted(want - capabilities))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power.py -q`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add crr/core/power.py tests/test_power.py
git commit -m "feat(core): unmet() names capabilities the platform lacks"
```

---

### Task 4: Ports — `PowerSource` and `PowerHolder`

**Files:**
- Modify: `crr/core/ports.py`
- Test: `tests/test_layering.py` (verify no new import direction), no new test file needed

**Interfaces:**
- Consumes: nothing
- Produces: `PowerSource` and `PowerHolder` Protocols with the exact signatures below, used by Tasks 5–9

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power.py`:

```python
def test_ports_declare_the_power_protocols():
    # The adapters in later tasks are checked against these signatures;
    # a rename here without a rename there is a silent breakage, because
    # Protocols are structural and nothing fails at import time.
    import inspect
    from crr.core import ports
    assert hasattr(ports, "PowerSource")
    assert hasattr(ports, "PowerHolder")
    assert list(inspect.signature(ports.PowerHolder.hold).parameters) == [
        "self", "want", "reason"]
    assert list(inspect.signature(ports.PowerSource.on_ac).parameters) == [
        "self"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power.py -k ports -v`
Expected: FAIL with `AssertionError` on `hasattr(ports, "PowerSource")`

- [ ] **Step 3: Write minimal implementation**

Append to `crr/core/ports.py`:

```python
@runtime_checkable
class PowerSource(Protocol):
    """Is this machine on mains power?

    Tri-state ON PURPOSE. A machine with no battery device is a desktop —
    that is a known ``True``, not an unknown. ``None`` means the probe
    failed, and per the spine principle that must never be turned into
    either answer by a consumer.
    """

    def on_ac(self) -> bool | None: ...


@runtime_checkable
class PowerHolder(Protocol):
    """Keep the machine awake / un-restartable while held.

    Implementations hold via a CHILD PROCESS whose lifetime is the hold,
    so the hold cannot outlive crr. Same reasoning as
    ``crr.adapters.locking``: crr's whole purpose is surviving processes
    that die badly, and a hold that leaks would block restarts with
    nothing left running to explain why.
    """

    def capabilities(self) -> frozenset[str]:
        """Which of {"sleep", "shutdown"} this platform can actually do."""

    def hold(self, want: frozenset[str], reason: str) -> None:
        """Start (or update) the hold. Idempotent for an unchanged want."""

    def release(self) -> None:
        """Drop the hold. Safe to call when nothing is held."""

    def held(self) -> frozenset[str]:
        """What is currently held."""
```

If `runtime_checkable` is not already imported in `ports.py`, add it to the
existing `from typing import ...` line.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power.py -q && .venv/bin/lint-imports`
Expected: 15 passed, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/core/ports.py tests/test_power.py
git commit -m "feat(ports): PowerSource and PowerHolder"
```

---

### Task 5: AC probe adapter

**Files:**
- Create: `crr/adapters/power_source.py`
- Test: `tests/test_power_adapters.py`

**Interfaces:**
- Consumes: `PowerSource` protocol from Task 4
- Produces: `SysfsPowerSource(root: Path = Path("/sys/class/power_supply"))` with `.on_ac()`; `MacPowerSource(timeout_seconds: float)` with `.on_ac()`; `_parse_pmset(text: str) -> bool | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_power_adapters.py`:

```python
"""Power adapters (spec 2026-08-12).

The AC probe is measured, not assumed: WSL2 passes the host battery
through sysfs (`/sys/class/power_supply/AC1/online`), which is why ONE
Linux adapter serves both native Linux and WSL.
"""

from pathlib import Path

import pytest

from crr.adapters.power_source import (MacPowerSource, SysfsPowerSource,
                                       _parse_pmset)


def _supply(root: Path, name: str, **files: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for key, value in files.items():
        (d / key).write_text(value + "\n", encoding="utf-8")


def test_mains_online_reads_as_on_ac(tmp_path):
    _supply(tmp_path, "AC1", type="Mains", online="1")
    _supply(tmp_path, "BAT1", type="Battery", status="Full")
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_mains_offline_reads_as_on_battery(tmp_path):
    _supply(tmp_path, "AC1", type="Mains", online="0")
    _supply(tmp_path, "BAT1", type="Battery", status="Discharging")
    assert SysfsPowerSource(tmp_path).on_ac() is False


def test_a_machine_with_no_power_supplies_is_a_desktop_not_an_unknown(tmp_path):
    # Known True, not None: a desktop is always on mains. Returning None
    # here would withhold the hold on every server and every VM.
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_a_battery_with_no_mains_device_falls_back_to_its_status(tmp_path):
    _supply(tmp_path, "BAT0", type="Battery", status="Discharging")
    assert SysfsPowerSource(tmp_path).on_ac() is False
    _supply(tmp_path, "BAT0", type="Battery", status="Charging")
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_an_unreadable_probe_is_unknown_not_a_guess(tmp_path):
    _supply(tmp_path, "AC1", type="Mains")   # no `online` file at all
    assert SysfsPowerSource(tmp_path).on_ac() is None


def test_a_missing_root_is_unknown(tmp_path):
    assert SysfsPowerSource(tmp_path / "nope").on_ac() is None


@pytest.mark.parametrize("text,expected", [
    ("Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged", True),
    ("Now drawing from 'Battery Power'\n -InternalBattery-0 82%", False),
    ("something unparseable", None),
    ("", None),
])
def test_pmset_parsing(text, expected):
    assert _parse_pmset(text) is expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crr.adapters.power_source'`

- [ ] **Step 3: Write minimal implementation**

Create `crr/adapters/power_source.py`:

```python
"""Is this machine on mains power? (implements crr.core.ports.PowerSource)

One Linux adapter covers native Linux AND WSL: measured on 2026-08-12,
WSL2 passes the Windows host's battery through sysfs
(`/sys/class/power_supply/AC1/online` read 1 while Windows reported
`BatteryStatus=2`). So the WSL case needs no interop here — unlike the
HOLD, which does.

Every failure path returns None rather than a guess. "I could not read the
power source" is not "on battery", and it is not "on AC".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SYSFS_ROOT = Path("/sys/class/power_supply")

# `upower`-free and `pmset`-free on Linux by design: reading two files
# beats shelling out on a path that runs every poll.
_CHARGING = ("charging", "full", "not charging")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


class SysfsPowerSource:
    """PowerSource from ``/sys/class/power_supply`` (Linux and WSL)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = SYSFS_ROOT if root is None else Path(root)

    def on_ac(self) -> bool | None:
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            return None
        # No entries at all: it's a desktop (always on mains)
        if not entries:
            return True
        mains: list[str] = []
        batteries: list[str] = []
        for entry in entries:
            kind = _read(entry / "type")
            if kind == "Mains":
                value = _read(entry / "online")
                if value is not None:
                    mains.append(value)
            elif kind == "Battery":
                status = _read(entry / "status")
                if status is not None:
                    batteries.append(status.lower())
        if mains:
            return any(v == "1" for v in mains)
        if batteries:
            # No mains device exposed (some laptops, some VMs): the
            # battery's own status still answers the question.
            return any(s in _CHARGING for s in batteries)
        # Entries exist but we couldn't read their state (e.g., unreadable files)
        return None


def _parse_pmset(text: str) -> bool | None:
    """True/False from ``pmset -g batt``; None when it does not say."""
    lowered = text.lower()
    if "'ac power'" in lowered:
        return True
    if "'battery power'" in lowered:
        return False
    return None


class MacPowerSource:
    """PowerSource from ``pmset -g batt`` (macOS)."""

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def on_ac(self) -> bool | None:
        try:
            result = subprocess.run(
                ["pmset", "-g", "batt"],
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        return _parse_pmset(result.stdout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -v && .venv/bin/lint-imports`
Expected: 11 passed, contract kept

- [ ] **Step 5: Verify against this real host**

Run: `.venv/bin/python -c "from crr.adapters.power_source import SysfsPowerSource; print(SysfsPowerSource().on_ac())"`
Expected: `True` while the laptop is plugged in (matches the measurement in
the spec). If it prints `None`, stop and investigate before continuing —
the adapter is wrong, not the machine.

- [ ] **Step 6: Commit**

```bash
git add crr/adapters/power_source.py tests/test_power_adapters.py
git commit -m "feat(adapters): AC probe, tri-state, one path for Linux and WSL"
```

---

### Task 6: Linux holder — and the lid safety check

**Files:**
- Create: `crr/adapters/power_hold_linux.py`
- Test: `tests/test_power_adapters.py`

**Interfaces:**
- Consumes: `PowerHolder` protocol (Task 4)
- Produces: `LinuxPowerHolder(conf_root: Path | None = None, spawn=subprocess.Popen)` with `.capabilities()`, `.hold(want, reason)`, `.release()`, `.held()`, `.withheld()`; module functions `inhibit_argv(want, reason) -> list[str]`, `lid_is_exempt(conf_text: str) -> bool` (pure single-text parser), `logind_sources(root) -> tuple[list[Path], bool]` and `lid_exemption(root) -> bool | None` (the effective config across main files AND drop-ins; `None` = unknown)

**READ THIS BEFORE WRITING CODE.** An earlier draft of the spec specified
`--what=idle` and a test *enforcing* that `sleep` never appear. Both were
wrong, and the wrong version reads as more obviously correct:

- `IdleAction=` defaults to `ignore`, so an `idle` lock inhibits a mechanism
  that is switched off. It holds successfully and protects nothing.
- `LidSwitchIgnoreInhibited=` defaults to `yes`, so a `sleep` lock does
  **not** block the lid.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power_adapters.py`:

```python
from crr.adapters.power_hold_linux import (LinuxPowerHolder, inhibit_argv,
                                           lid_is_exempt)


def test_inhibit_asks_for_sleep_not_idle():
    # `idle` inhibits logind's IdleAction, which defaults to `ignore` — it
    # would hold successfully and protect NOTHING. `sleep` is what GNOME
    # and KDE's idle-suspend actually goes through. This is the opposite
    # of the obvious choice; see the spec before "fixing" it.
    argv = inhibit_argv(frozenset({"sleep"}), "crr: 2 Claude sessions live")
    what = argv[argv.index("--what") + 1] if "--what" in argv else ""
    joined = " ".join(argv)
    assert "sleep" in what, f"must inhibit sleep, got {joined}"
    assert "idle" not in what, (
        "idle inhibits IdleAction, which defaults to ignore — a hold that "
        f"protects nothing. Got {joined}")


def test_inhibit_adds_shutdown_only_when_asked():
    both = inhibit_argv(frozenset({"sleep", "shutdown"}), "r")
    what = both[both.index("--what") + 1]
    assert set(what.split(":")) == {"sleep", "shutdown"}


def test_inhibit_is_block_mode_and_carries_the_reason():
    argv = inhibit_argv(frozenset({"sleep"}), "crr: 1 Claude session live")
    assert argv[0] == "systemd-inhibit"
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "block"
    assert "crr: 1 Claude session live" in argv


@pytest.mark.parametrize("conf,exempt", [
    ("", True),                                    # unset -> default yes
    ("#LidSwitchIgnoreInhibited=yes\n", True),     # commented -> default
    ("LidSwitchIgnoreInhibited=yes\n", True),
    ("LidSwitchIgnoreInhibited=no\n", False),
    ("[Login]\nLidSwitchIgnoreInhibited = no\n", False),
])
def test_lid_exemption_is_read_not_assumed(conf, exempt):
    assert lid_is_exempt(conf) is exempt


def _logind(root: Path, rel: str, text: str) -> Path:
    """Write a logind config source at ``rel`` under a fake filesystem root.

    ``rel`` is always relative -- `root / "/etc/..."` would silently
    discard `root` and point at the REAL host config.
    """
    assert not rel.startswith("/"), "rel must be relative or root is discarded"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _LiveProc:
    """A spawn result that stays alive and reaps cleanly."""

    def __init__(self): self.terminated = False
    def poll(self): return None
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): return 0


def test_holder_refuses_to_block_sleep_when_the_lid_is_not_exempt(tmp_path):
    # The builder's hard requirement is that closing the lid always
    # sleeps. On a host that has turned the default off, a sleep lock
    # would break that, so crr withholds instead.
    _logind(tmp_path, "etc/systemd/logind.conf",
            "LidSwitchIgnoreInhibited=no\n")
    spawned = []
    holder = LinuxPowerHolder(conf_root=tmp_path,
                              spawn=lambda argv, **kw: spawned.append(argv))
    holder.hold(frozenset({"sleep"}), "r")
    assert spawned == [], "blocked sleep on a host where that blocks the lid"
    assert holder.held() == frozenset()


def test_holder_still_blocks_shutdown_when_the_lid_is_not_exempt(tmp_path):
    # Only the sleep half is unsafe there; shutdown is unaffected by lid
    # handling, so withholding it too would be over-correction.
    _logind(tmp_path, "etc/systemd/logind.conf",
            "LidSwitchIgnoreInhibited=no\n")
    spawned = []

    def _spawn(argv, **kw):
        spawned.append(argv)
        return _LiveProc()

    holder = LinuxPowerHolder(conf_root=tmp_path, spawn=_spawn)
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"shutdown"})
    what = spawned[0][spawned[0].index("--what") + 1]
    assert what == "shutdown"


def test_capabilities_are_both_on_linux(tmp_path):
    holder = LinuxPowerHolder(conf_root=tmp_path / "absent")
    assert holder.capabilities() == frozenset({"sleep", "shutdown"})


# --- the effective logind config, not just one file -----------------------
# logind's RECOMMENDED override mechanism is a drop-in, not an edit to
# logind.conf. A holder that reads only /etc/systemd/logind.conf therefore
# reads a file the host may have deliberately overridden -- and the failure
# is the one thing that must never happen: `LidSwitchIgnoreInhibited=no` in
# a drop-in, read as the compiled-in `yes`, so crr holds `sleep` and closing
# the lid stops suspending the machine.

@pytest.mark.parametrize("rel", [
    "etc/systemd/logind.conf.d/90-crr.conf",
    "run/systemd/logind.conf.d/90-crr.conf",
    "usr/lib/systemd/logind.conf.d/90-crr.conf",
])
def test_a_dropin_saying_no_withholds_sleep_from_every_dropin_dir(tmp_path, rel):
    # A stock main file that never mentions the key, plus a drop-in that
    # turns the exemption off -- exactly the shape a distro package ships.
    _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n#NAutoVTs=6\n")
    _logind(tmp_path, rel, "[Login]\nLidSwitchIgnoreInhibited=no\n")
    assert lid_exemption(tmp_path) is False, (
        f"{rel} overrides the main file; missing it means crr holds sleep "
        "and closing the lid no longer suspends")


def test_the_usr_lib_main_conf_is_a_source_too(tmp_path):
    # On Fedora-likes /usr/lib/systemd/logind.conf is the ONLY main file;
    # /etc/systemd/logind.conf does not exist at all.
    _logind(tmp_path, "usr/lib/systemd/logind.conf",
            "[Login]\nLidSwitchIgnoreInhibited=no\n")
    assert lid_exemption(tmp_path) is False


def test_a_dropin_that_says_nothing_leaves_the_default_alone(tmp_path):
    # The real drop-in on this host (unattended-upgrades) sets an unrelated
    # key. Treating any drop-in's mere existence as "not exempt" would make
    # crr refuse to block sleep on every Ubuntu box -- protecting nothing,
    # from the other direction.
    _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    _logind(tmp_path, "usr/lib/systemd/logind.conf.d/10-maxdelay.conf",
            "[Login]\nInhibitDelayMaxSec=30\n")
    assert lid_exemption(tmp_path) is True


def test_no_config_source_at_all_is_the_compiled_in_default_not_unknown(tmp_path):
    # KNOWN, not unknown: with no config anywhere, logind uses its
    # compiled-in LidSwitchIgnoreInhibited=yes. Mirrors the AC probe's
    # empty-directory-is-a-desktop case.
    assert lid_exemption(tmp_path) is True


def test_a_source_that_exists_but_cannot_be_read_is_unknown_not_safe(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    conf = _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    conf.chmod(0o000)
    try:
        assert lid_exemption(tmp_path) is None, (
            "never read the config is not the same as safe to hold sleep")
    finally:
        conf.chmod(0o644)


def test_a_dropin_dir_that_cannot_be_listed_is_unknown_not_empty(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    d = tmp_path / "etc/systemd/logind.conf.d"
    d.mkdir(parents=True)
    d.chmod(0o000)
    try:
        assert lid_exemption(tmp_path) is None, (
            "an unlistable drop-in dir is unknown; reporting it as empty is "
            "the same defect as reporting an unreadable file as exempt")
    finally:
        d.chmod(0o755)


def test_a_definite_no_beats_an_unreadable_sibling(tmp_path):
    # Precedence-proof by construction: ANY source saying no wins, so the
    # answer never depends on implementing logind's precedence rules.
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    _logind(tmp_path, "etc/systemd/logind.conf",
            "LidSwitchIgnoreInhibited=no\n")
    other = _logind(tmp_path, "usr/lib/systemd/logind.conf", "[Login]\n")
    other.chmod(0o000)
    try:
        assert lid_exemption(tmp_path) is False
    finally:
        other.chmod(0o644)


def test_the_collector_finds_every_source_logind_would_read(tmp_path):
    main = _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    lib_main = _logind(tmp_path, "usr/lib/systemd/logind.conf", "[Login]\n")
    dropin = _logind(tmp_path, "run/systemd/logind.conf.d/50-x.conf", "[Login]\n")
    _logind(tmp_path, "run/systemd/logind.conf.d/notes.txt", "ignored\n")
    paths, complete = logind_sources(tmp_path)
    assert complete is True
    found = set(paths)
    assert {main, lib_main, dropin} <= found
    assert not any(p.name.endswith(".txt") for p in paths), (
        "logind reads *.conf drop-ins only")


def test_the_real_host_config_is_exempt_so_crr_is_not_over_corrected(tmp_path):
    # Guard against the opposite failure: an implementation that returns
    # False/None on a stock box never blocks sleep anywhere. Measured
    # 2026-08-13 on this host -- a readable /etc/systemd/logind.conf plus
    # /usr/lib/systemd/logind.conf.d/unattended-upgrades-logind-maxdelay.conf,
    # neither setting the key.
    if not Path("/etc/systemd/logind.conf").exists():
        pytest.skip("no logind config on this host")
    if lid_exemption(Path("/")) is None:
        pytest.skip("host logind config is unreadable by this user")
    assert lid_exemption(Path("/")) is True


def test_the_withheld_reason_does_not_claim_a_setting_it_never_read(tmp_path):
    # Two different withholdings with two different reasons. Reporting
    # "this host sets LidSwitchIgnoreInhibited=no" when the config could
    # not be read is a confident claim about a fact never established.
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    conf = _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    conf.chmod(0o000)
    try:
        holder = LinuxPowerHolder(conf_root=tmp_path,
                                  spawn=lambda argv, **kw: _LiveProc())
        holder.hold(frozenset({"sleep"}), "r")
    finally:
        conf.chmod(0o644)
    assert holder.held() == frozenset()
    reason = holder.withheld() or ""
    assert "LidSwitchIgnoreInhibited=no" not in reason, (
        f"claims a setting it never read: {reason!r}")
    assert "read" in reason or "unknown" in reason, reason


# --- the spawn either worked or it did not --------------------------------

def test_a_systemd_inhibit_that_fails_is_not_reported_as_a_hold(tmp_path):
    # Measured on this host (WSL, no logind session), 2026-08-13:
    #   systemd-inhibit --what=sleep --mode=block --who=crr --why=x sleep 1
    #   -> stderr "Failed to inhibit: Access denied", exit 1, in
    #   milliseconds.
    # With stderr=DEVNULL and an unconditional `self._held = effective`,
    # held() reported the full set, then reported empty with withheld()
    # None -- no reason recorded anywhere, because the stderr that
    # explained it was discarded at the source.
    fail = [_sys.executable, "-c",
            "import sys; sys.stderr.write('Failed to inhibit: Access denied\\n');"
            " sys.exit(1)"]

    def _spawn(argv, **kw):
        return _sp.Popen(fail, **kw)   # a REAL process, real exit, real stderr

    holder = LinuxPowerHolder(conf_root=tmp_path, spawn=_spawn)
    holder.hold(frozenset({"sleep"}), "r")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and holder.held():
        time.sleep(0.02)
    assert holder.held() == frozenset(), (
        "reported a hold from a systemd-inhibit that had already exited 1")
    reason = holder.withheld() or ""
    assert "Access denied" in reason, (
        f"the stderr that explains the failure must survive: {reason!r}")
    holder.release()


def test_release_never_reads_stderr_from_a_child_it_could_not_reap(tmp_path):
    # `stream.read()` on a LIVE child's pipe does not raise -- it blocks
    # until EOF, i.e. forever, wedging the poll loop this adapter exists to
    # stay off. So the drain must be gated on a CONFIRMED exit, not run
    # unconditionally after a wait() that may have timed out.
    # A raising stub would NOT discriminate: _drain_stderr catches
    # Exception, so the raise is swallowed and the broken version passes.
    # The real failure is a BLOCK, so record the call instead.
    class _Stderr:
        def __init__(self): self.read_called = False

        def read(self):
            self.read_called = True      # in reality: blocks until EOF
            return b""

        def close(self): pass

    class _Unreapable:
        def __init__(self): self.stderr = _Stderr()
        def poll(self): return None
        def terminate(self): pass

        def wait(self, timeout=None):
            raise _sp.TimeoutExpired(cmd="systemd-inhibit", timeout=timeout)

    proc = _Unreapable()
    holder = LinuxPowerHolder(conf_root=tmp_path,
                              spawn=lambda argv, **kw: proc)
    holder.hold(frozenset({"sleep"}), "r")
    holder.release()
    assert not proc.stderr.read_called, (
        "drained stderr from a child that wait() never confirmed dead -- "
        "that read blocks until EOF, wedging the poll loop forever")
    assert holder.held() == frozenset()


def test_a_live_inhibit_still_reports_its_hold(tmp_path):
    # The reap must not turn a WORKING hold into a withheld one.
    stay = [_sys.executable, "-c", "import time; time.sleep(30)"]
    holder = LinuxPowerHolder(conf_root=tmp_path,
                              spawn=lambda argv, **kw: _sp.Popen(stay, **kw))
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    try:
        assert holder.held() == frozenset({"sleep", "shutdown"})
        assert holder.withheld() is None
    finally:
        holder.release()
    assert holder.held() == frozenset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -k "inhibit or lid or holder or capabilities" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crr.adapters.power_hold_linux'`

- [ ] **Step 3: Write minimal implementation**

Create `crr/adapters/power_hold_linux.py`:

```python
"""Linux power hold via systemd-inhibit (implements ports.PowerHolder).

**It is `--what=sleep`, not `--what=idle`.** This is the opposite of the
obvious choice and an earlier draft of the design got it backwards, so the
reasoning lives here rather than in a commit message. Per logind.conf(5):

- ``IdleAction=`` defaults to ``ignore``. An ``idle`` lock therefore
  inhibits a mechanism that is switched off on a default system: it would
  hold successfully, report success, and protect nothing.
- ``LidSwitchIgnoreInhibited=`` defaults to ``yes``. A ``sleep`` lock
  therefore does NOT block closing the lid, which is the builder's hard
  requirement.

GNOME and KDE suspend on idle by asking logind to ``Suspend()`` as an
unprivileged user, and that is exactly what a ``sleep`` lock inhibits.

The one case that breaks this: a host that has set
``LidSwitchIgnoreInhibited=no``. There a sleep lock WOULD block the lid, so
``hold()`` withholds the sleep half and says so rather than quietly
violating the requirement.

**That setting is read from the EFFECTIVE config, not from one file.**
logind's recommended override mechanism is a drop-in
(``{/etc,/run,/usr/lib}/systemd/logind.conf.d/*.conf``), not an edit to
``logind.conf``, and on Fedora-likes ``/usr/lib/systemd/logind.conf`` is
the only main file that exists. Reading ``/etc/systemd/logind.conf`` alone
means a host that turned the exemption off in a drop-in reads back as the
compiled-in ``yes`` -- crr holds ``sleep`` and closing the lid stops
suspending the machine, which is the one outcome this design must never
produce.

The rule here is deliberately NOT logind's precedence algorithm: **if any
source says a falsey value, withhold sleep.** That is precedence-proof
without implementing precedence, and it errs toward "do not touch the
lid". ``systemd-analyze cat-config`` would give the truly effective
config, but this runs on a poll path and shelling out there is what the
rest of these adapters exist to avoid.

Three-way, mirroring ``power_source.SysfsPowerSource.on_ac()``:

- No config source exists at all -> logind's compiled-in default (``yes``)
  applies. That is a KNOWN True, not an unknown.
- A source exists but cannot be read (or a drop-in directory cannot even be
  listed) -> unknown, ``None``. Never a positive "safe to hold".
- Otherwise the parsed answer.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from crr.adapters._proc import (FORCE_WAIT_SECONDS, RELEASE_WAIT_SECONDS,
                                release_child, signal_child)

# Relative on purpose: joined onto an injectable root so the whole set is
# testable against a fake filesystem. `root / "/etc/..."` would silently
# discard root and read the real host.
LOGIND_MAIN = (
    "etc/systemd/logind.conf",
    # The only main file on Fedora-likes; absent on Debian-likes.
    "usr/lib/systemd/logind.conf",
)
LOGIND_DROPIN_DIRS = (
    "etc/systemd/logind.conf.d",
    "run/systemd/logind.conf.d",
    "usr/lib/systemd/logind.conf.d",
)

_LID_RE = re.compile(
    r"^\s*LidSwitchIgnoreInhibited\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE
)
_FALSEY = ("no", "false", "0", "off")


def lid_is_exempt(conf_text: str) -> bool:
    """True when closing the lid sleeps even while an inhibitor is held.

    The pure single-text parser. Defaults to True because logind's own
    default is ``yes``; a commented line is not a setting, and ``^\\s*``
    under ``re.MULTILINE`` already cannot match ``#LidSwitch...``, so
    ``match is None`` covers the commented case with no extra guard.
    """
    match = None
    for candidate in _LID_RE.finditer(conf_text):
        match = candidate
    if match is None:
        return True
    return match.group(1).strip().lower() not in _FALSEY


def logind_sources(root: Path) -> tuple[list[Path], bool]:
    """Every file logind would read under ``root``, and whether the walk
    was complete.

    Returns ``(candidate_paths, complete)``. The main files are returned as
    candidates whether or not they exist -- the reader discriminates
    "absent" from "unreadable", which a bare ``list[Path]`` cannot carry.
    ``complete`` is False when a drop-in directory exists but could not be
    listed: an unlistable directory is unknown, and returning it as empty
    would be the same defect this whole function fixes.
    """
    root = Path(root)
    paths = [root / rel for rel in LOGIND_MAIN]
    complete = True
    for rel in LOGIND_DROPIN_DIRS:
        try:
            entries = sorted((root / rel).iterdir())
        except FileNotFoundError:
            continue                      # no such drop-in dir: not a source
        except OSError:
            complete = False              # it exists; we just cannot see in
            continue
        paths.extend(e for e in entries if e.name.endswith(".conf"))
    return paths, complete


def lid_exemption(root: Path) -> bool | None:
    """Three-way lid exemption across every logind config source.

    ``True`` exempt (a sleep inhibitor leaves the lid alone), ``False`` not
    exempt (a sleep inhibitor WOULD block the lid), ``None`` unknown.
    """
    paths, complete = logind_sources(root)
    unknown = not complete
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue                      # not a source on this distro
        except OSError:
            unknown = True                # exists, unreadable: not a "yes"
            continue
        if not lid_is_exempt(text):
            # A definite falsey value anywhere wins outright, so the answer
            # never depends on precedence -- and it beats an unreadable
            # sibling, because False and None both withhold anyway.
            return False
    if unknown:
        return None
    # Either every source is silent on the key or there are no sources at
    # all; both land on logind's compiled-in default, which is `yes`.
    return True


def inhibit_argv(want: frozenset[str], reason: str) -> list[str]:
    """The systemd-inhibit command line for ``want``."""
    return [
        "systemd-inhibit",
        "--what", ":".join(sorted(want)),
        "--mode", "block",
        "--who", "crr",
        "--why", reason,
        # Sleeps forever; the hold ends when this child is killed, which is
        # the whole crash-safety property.
        "sleep", "infinity",
    ]


class LinuxPowerHolder:
    """Holds via a systemd-inhibit child process."""

    def __init__(self, conf_root: Path | None = None, spawn=None) -> None:
        # The filesystem root the logind config is read from. Injectable so
        # the whole source set (main files AND drop-in dirs) is testable.
        self._root = Path("/") if conf_root is None else Path(conf_root)
        self._spawn = spawn or (lambda argv, **kw: subprocess.Popen(argv, **kw))
        self._proc = None
        self._held: frozenset[str] = frozenset()
        self._withheld: str | None = None

    def capabilities(self) -> frozenset[str]:
        return frozenset({"sleep", "shutdown"})

    def withheld(self) -> str | None:
        """Why part of the request was dropped, for doctor."""
        return self._withheld

    def hold(self, want: frozenset[str], reason: str) -> None:
        self._withheld = None
        effective = set(want)
        if "sleep" in effective:
            exempt = lid_exemption(self._root)
            if exempt is not True:
                effective.discard("sleep")
                # Two different withholdings need two different reasons:
                # saying "this host sets LidSwitchIgnoreInhibited=no" when
                # the config could not be read asserts a fact never
                # established.
                self._withheld = (
                    "not blocking sleep: this host sets "
                    "LidSwitchIgnoreInhibited=no (in logind.conf or a "
                    "drop-in), so a sleep inhibitor would also block "
                    "closing the lid"
                    if exempt is False else
                    "not blocking sleep: could not read this host's logind "
                    "configuration, so whether a sleep inhibitor would also "
                    "block closing the lid is unknown"
                )
        effective_fs = frozenset(effective)
        if effective_fs == self._held and self._alive():
            return
        self.release()
        if not effective_fs:
            return
        # stderr is CAPTURED, not discarded. systemd-inhibit exits nonzero
        # in milliseconds on a host with no logind session or a polkit
        # denial (measured on this WSL box, 2026-08-13: "Failed to inhibit:
        # Access denied", exit 1), and with DEVNULL the one line that
        # explains why was destroyed at the source -- held() went full set,
        # then empty, with withheld() None and no reason recorded anywhere.
        self._proc = self._spawn(
            inhibit_argv(effective_fs, reason),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._held = effective_fs
        # Poll once: a denied inhibit is usually already gone. It often is
        # not scheduled yet either, which is why held() reaps too rather
        # than trusting this one call.
        self._reap_if_dead()

    def _reap_if_dead(self) -> None:
        """Drop the claim (and record why) if the child has already exited."""
        proc = self._proc
        if proc is None or proc.poll() is None:
            return
        detail = self._drain_stderr(proc)
        code = proc.returncode
        if code:
            self._withheld = (
                f"systemd-inhibit exited {code} without holding anything"
                + (f": {detail}" if detail else "")
            )
        # Drop the handle so this runs exactly ONCE. It is called from both
        # hold() and every held(), and a second pass would read a stderr
        # stream it already closed -- overwriting the recorded reason with
        # a detail-free one, i.e. destroying the explanation a second time.
        # The child is exited and already reaped by poll(); there is
        # nothing left to do with it.
        self._proc = None
        self._held = frozenset()

    @staticmethod
    def _drain_stderr(proc) -> str:
        """Read stderr from an EXITED child. Never call this on a live one:
        it would block the poll path forever."""
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return ""
        try:
            raw = stream.read() or b""
        except Exception:
            return ""
        finally:
            try:
                stream.close()          # or an fd leaks per hold/release
            except Exception:
                pass
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return " ".join(raw.split())

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def held(self) -> frozenset[str]:
        self._reap_if_dead()
        return self._held if self._alive() else frozenset()

    def release(self) -> None:
        """Signal, then escalate until the child is CONFIRMED reaped.

        The first version called ``terminate()`` then ``wait(5)`` ONCE and
        cleared ``_proc``/``_held`` unconditionally -- even when the wait
        raised. A child that ignores SIGTERM left crr with no handle to a
        process that may still hold ``systemd-inhibit``'s lock,
        permanently uncleanable, ``held()`` reporting nothing held (issue
        #77). The same defect was fixed on the Windows side; this shares
        that fix's ladder (``crr.adapters._proc.release_child``) rather
        than keeping two copies that would only drift apart again.

        There is no stdin to close here (unlike the Windows holder):
        ``terminate()`` IS the graceful request, sent up front so the
        ladder's own first wait is checking whether that already worked
        rather than waiting out the whole first-wait budget for nothing.

        The handle is dropped only on confirmation. Deliberately no
        ``finally:`` around the bookkeeping -- that is exactly the shape
        that reinstates the bug.
        """
        proc = self._proc
        if proc is None:
            self._held = frozenset()
            return
        signal_child(proc, "terminate")
        if not release_child(proc, RELEASE_WAIT_SECONDS, FORCE_WAIT_SECONDS):
            # Neither the initial terminate nor the escalation to kill
            # confirmed it dead. KEEP the handle and KEEP reporting the
            # set: a live child may genuinely still be holding, and the
            # next hold() retries release(). Reporting an empty hold here
            # would be the same lie, just quieter.
            return
        # Gated on a CONFIRMED exit. `stream.read()` on a live child's
        # pipe does not raise -- it blocks until EOF, i.e. forever,
        # wedging the poll loop. An unreaped child's fd is closed by
        # the OS when the Popen object is dropped, so skipping the
        # drain here costs nothing.
        self._drain_stderr(proc)
        self._proc = None
        self._held = frozenset()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -v && .venv/bin/lint-imports`
Expected: all pass, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/power_hold_linux.py tests/test_power_adapters.py
git commit -m "feat(adapters): Linux hold via systemd-inhibit --what=sleep

sleep, not idle: IdleAction defaults to ignore so an idle lock protects
nothing, and LidSwitchIgnoreInhibited defaults to yes so a sleep lock
leaves the lid alone. Withholds the sleep half entirely on a host that
has turned that default off."
```

---

### Task 7: macOS holder

**Files:**
- Create: `crr/adapters/power_hold_macos.py`
- Test: `tests/test_power_adapters.py`

**Interfaces:**
- Consumes: `PowerHolder` protocol (Task 4)
- Produces: `MacPowerHolder(spawn=None)` with the four `PowerHolder` methods; `caffeinate_argv() -> list[str]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power_adapters.py`:

```python
from crr.adapters.power_hold_macos import MacPowerHolder, caffeinate_argv


def test_macos_can_hold_sleep_but_not_shutdown():
    # Not an omission. A launch daemon cannot block a macOS shutdown at
    # all: the cancellable notifications do not reach daemons, and only a
    # GUI app in the login session can delay one. Deferred by the spec.
    assert MacPowerHolder().capabilities() == frozenset({"sleep"})


def test_caffeinate_holds_idle_only_so_the_lid_still_sleeps():
    argv = caffeinate_argv()
    assert argv[0] == "caffeinate"
    assert "-i" in argv
    assert "-s" not in argv, "-s would fight the lid; idle only"


def test_macos_holder_ignores_a_shutdown_request_it_cannot_serve():
    spawned = []

    class _P:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    holder = MacPowerHolder(spawn=lambda argv, **kw: spawned.append(argv) or _P())
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"sleep"})
    assert len(spawned) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -k macos -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `crr/adapters/power_hold_macos.py`:

```python
"""macOS power hold via caffeinate (implements ports.PowerHolder).

Sleep only. A launch daemon cannot block a macOS shutdown: the
cancellable stage is before ``kLWPointOfNoReturn`` and those
notifications do not reach daemons — only a GUI app in the login session
can delay one, which the spec defers. ``capabilities()`` says so rather
than accepting a shutdown request and silently doing nothing.

``-i`` (idle) and deliberately NOT ``-s``: the lid must keep working.
"""

from __future__ import annotations

import subprocess


def caffeinate_argv() -> list[str]:
    return ["caffeinate", "-i"]


class MacPowerHolder:
    def __init__(self, spawn=None) -> None:
        self._spawn = spawn or (lambda argv, **kw: subprocess.Popen(argv, **kw))
        self._proc = None
        self._held: frozenset[str] = frozenset()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"sleep"})

    def hold(self, want: frozenset[str], reason: str) -> None:
        effective = frozenset(want) & self.capabilities()
        if effective == self._held and self._alive():
            return
        self.release()
        if not effective:
            return
        self._proc = self._spawn(
            caffeinate_argv(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._held = effective

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def held(self) -> frozenset[str]:
        return self._held if self._alive() else frozenset()

    def release(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        self._held = frozenset()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/power_hold_macos.py tests/test_power_adapters.py
git commit -m "feat(adapters): macOS hold via caffeinate -i, sleep only"
```

---

### Task 8: Windows holder — both locks, and the orphan defence

**Files:**
- Create: `crr/adapters/power_hold_windows.py`
- Test: `tests/test_power_adapters.py`

**Interfaces:**
- Consumes: `PowerHolder` protocol (Task 4)
- Produces: `WindowsPowerHolder(spawn=None, max_hours=DEFAULTS["power_block_max_hours"])` with the four methods; `holder_script(want: frozenset[str], reason: str, max_hours=DEFAULTS["power_block_max_hours"]) -> str`; `holder_argv() -> list[str]`

**The most dangerous task in the plan.** A Windows interop child does not
die with its WSL parent. An orphan would block restarts forever with no crr
running to explain it. The script must exit when its stdin closes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power_adapters.py`:

```python
import subprocess as _sp
import sys as _sys

from crr.adapters.power_hold_windows import (WindowsPowerHolder,
                                             holder_argv, holder_script)


def test_windows_claims_both_capabilities():
    assert WindowsPowerHolder().capabilities() == frozenset(
        {"sleep", "shutdown"})


def test_script_sets_execution_state_for_sleep():
    s = holder_script(frozenset({"sleep"}), "crr: 1 Claude session live")
    assert "SetThreadExecutionState" in s
    assert "0x80000001" in s or ("ES_CONTINUOUS" in s and "ES_SYSTEM_REQUIRED" in s)
    assert "ShutdownBlockReasonCreate" not in s


def test_script_registers_a_block_reason_for_shutdown():
    s = holder_script(frozenset({"sleep", "shutdown"}), "crr: 2 live")
    assert "ShutdownBlockReasonCreate" in s
    assert "crr: 2 live" in s


def test_script_exits_when_stdin_closes():
    # THE orphan defence. Without this a killed crr leaves a PowerShell
    # holding a shutdown block forever, and the user has a machine that
    # refuses to restart with nothing left running to explain why.
    #
    # NOTE: this assertion was corrected after implementation. The
    # shipped script does NOT use [Console]::In / ReadLine -- that reader
    # wraps a SyncTextReader and blocks the calling thread synchronously
    # even through its "Async" method, measured live 2026-08-13 (see
    # `crr/adapters/power_hold_windows.py`'s module docstring, point 3).
    # The orphan defence is [Console]::OpenStandardInput() (the raw,
    # unwrapped stream) read via Stream.ReadAsync.
    s = holder_script(frozenset({"sleep"}), "r")
    assert "OpenStandardInput" in s and "ReadAsync" in s, (
        "no async stdin read: an orphan would hold forever")


def test_script_self_releases_after_the_cap():
    # `"12" in s` was the original assertion and it proved nothing: the
    # emitted script carries a leading `# max_hours=12` comment, so a
    # completely dead deadline still passed. Assert the millisecond value
    # actually inside the bounded wait.
    s = holder_script(frozenset({"sleep"}), "r", max_hours=12)
    match = re.search(r"WaitAny\(\s*@\(\$readTask\)\s*,\s*\[int\](\d+)\s*\)", s)
    assert match is not None, "no bounded wait carrying a deadline at all"
    assert int(match.group(1)) == 12 * 3600 * 1000, (
        "the cap must reach the wait, not just the comment above it")


def test_argv_runs_powershell_noninteractively_with_stdin_open():
    argv = holder_argv()
    assert argv[0] == "powershell.exe"
    assert "-NoProfile" in argv
    assert "-Command" in argv
    assert "-NonInteractive" not in argv, (
        "the holder READS stdin as its liveness signal; -NonInteractive "
        "would defeat the orphan defence")


def test_a_stdin_eof_child_exits_promptly():
    # Platform-independent proof of the MECHANISM the Windows script uses:
    # a child reading stdin to EOF must exit when the pipe closes. The
    # PowerShell equivalent is asserted by inspection above; this asserts
    # the pattern actually terminates a process.
    proc = _sp.Popen([_sys.executable, "-c",
                      "import sys; sys.stdin.read()"], stdin=_sp.PIPE)
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -k windows_ -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

> **This block was corrected after implementation.** The version
> originally here failed its own Step-1 tests (the `0x80000001` literal
> never appeared in the emitted text, and the `$sig` block declared
> `ShutdownBlockReasonCreate` even on a sleep-only hold), and after those
> were fixed a **Critical** review finding caught a second defect: the
> `while (deadline) { ReadLine() }` loop only re-checks the deadline
> *between* completed reads, so `max_hours` never actually fired as a
> backstop. Chasing that fix down uncovered two more bugs that are
> unrelated to the WaitAny loop itself: a `@" ... "@` here-string
> silently executes nothing at all when piped to `-Command -` over a
> non-console stdin, and — the one that actually explains the observed
> failure — reading raw stdin from *inside* a script that `-Command -` is
> itself still consuming from the same kernel pipe races the two readers,
> silently corrupting trailing PowerShell text by one dropped character
> and ending the hold hours early while `held()` keeps reporting success.
> See `crr/adapters/power_hold_windows.py`'s module docstring for the
> full, dated account of each defect and how it was measured live against
> the real Windows host. The block below is the code that actually
> passed, including the STOP CONDITION.

Create `crr/adapters/power_hold_windows.py`:

```python
"""Windows power hold from WSL, via one PowerShell child.

Both locks live in ONE process so there is one lifetime to reason about:
``SetThreadExecutionState`` for idle sleep, and a window's
``ShutdownBlockReasonCreate`` for restart. Both were measured callable
UNELEVATED from WSL on 2026-08-12.

Seven things this file must never lose:

1. **The stdin-EOF exit.** A Windows interop child does NOT die with its
   WSL parent. Without this wait, a killed crr leaves a PowerShell holding
   a shutdown block forever — a machine that refuses to restart with
   nothing left running to explain why. That is the exact class of
   unexplained behaviour crr exists to eliminate, so the mechanism (not a
   timer) is the primary defence.
2. **``ES_SYSTEM_REQUIRED`` only, never ``ES_DISPLAY_REQUIRED``.** The lid
   must keep working and the screen must be allowed to turn off.
3. **The deadline is ONE bounded async wait on the RAW stdin stream,
   never a loop around a blocking read, and never ``[Console]::In``.**
   ``[Console]::In.ReadLine()`` blocks synchronously, so a
   ``while (deadline) { ReadLine() }`` loop only re-checks the deadline
   *between* completed reads. Since ``hold()`` writes the script once and
   never sends a second line, the process would enter ``ReadLine`` a
   single time and block there for the rest of its life — the deadline
   would never re-fire and ``max_hours`` would be dead code wearing a
   passing string-match test. Confirmed live on 2026-08-12: a holder
   spawned with ``max_hours=0.0006`` (~2.16s), stdin left open with no
   further writes, was still alive at t=25s.

   The first fix attempt swapped in ``[Console]::In.ReadLineAsync()``
   awaited via ``Task.Wait(ms)``. That is ALSO wrong, and was caught the
   same way: measured live on 2026-08-13, the assignment
   ``$readTask = [Console]::In.ReadLineAsync()`` itself did not return
   control for 30+ seconds with stdin held open and no data sent — so
   ``.Wait(ms)`` never even started timing. ``[Console]::In`` wraps a
   ``SyncTextReader``, and ``ReadLineAsync()`` on that reader runs
   synchronously on the calling thread despite returning a ``Task``. The
   working fix reads the RAW stream from
   ``[Console]::OpenStandardInput()`` (unwrapped, genuinely async) via
   ``Stream.ReadAsync`` and bounds it with
   ``[System.Threading.Tasks.Task]::WaitAny(@($readTask), ms)``, which
   returns ``-1`` on timeout (the read stays pending, harmless — the
   process is exiting either way) or the completed index on EOF.
4. **The P/Invoke signature block is ONE PowerShell source line, never a
   here-string.** ``$sig = @" ... "@`` is what the original design and
   the first fix attempt both used. Measured live on 2026-08-13: fed
   through ``powershell.exe -NoProfile -Command -`` over a piped (not
   console) stdin, a ``@" ... "@`` block never executes ANYTHING in the
   script that contains it — not the here-string assignment, not any
   statement before or after it — even after real EOF, even holding
   stdin open for several seconds first. The failure is silent: exit code
   0, zero output, every single time, regardless of the here-string's
   content (confirmed with a trivial one-line body, not just the real
   DllImport signatures). Multi-line constructs that DON'T require the
   parser to buffer across lines (a plain multi-statement script, a
   single-line ``@($x)`` array subexpression) execute immediately and
   correctly over the same piped stdin; only the here-string's inherent
   "keep reading until a line starts with the closing token" parsing was
   silently broken here. The fix builds the C# signature block as ONE
   PowerShell line: a normal double-quoted string with embedded double
   quotes backtick-escaped (`` `" ``) and line breaks inserted via
   PowerShell's own `` `n `` escape, so nothing about the *PowerShell
   source* spans multiple lines even though the *string value* does.
5. **The script ends with an explicit exit, never falls off the end.**
   ``powershell.exe -Command -`` behaves like an interactive session: once
   it finishes running whatever was piped to it, it goes back to reading
   stdin for the *next command*, it does not quit. Confirmed live on
   2026-08-13 with full tracing: every statement in the script — sig
   build, Add-Type, both P/Invoke calls, the WaitAny deadline firing
   (idx=-1) or completing (EOF), both release calls — ran to completion
   in ~2.4s, and the *process* was still alive and reported so 30 seconds
   later, because after the last statement it just waited for another
   command on the same still-open stdin. ``[Environment]::Exit(0)`` as
   the final statement is what actually ends the process. Releasing the
   locks is necessary but not sufficient for "the process is gone" — both
   are required, and they are different lines of PowerShell.
6. **The whole script is ONE PowerShell statement, never multiple
   top-level lines.** This is the one that actually explains why a
   sleep-only hold worked throughout this file's history while a
   sleep+shutdown hold quietly self-released hours early. Our own
   ``$stdin.ReadAsync($buf, 0, 1)`` and ``-Command -``'s own read of "the
   rest of the piped script" pull from the SAME kernel pipe. Whenever
   PowerShell statements remained unparsed after the read (true for
   sleep+shutdown, which has release calls after the wait; not true for a
   bare sleep-only script with nothing left to run), the two reads raced
   for the same bytes. Measured live on 2026-08-13, 100% reproducible for
   a fixed script: the internal read sometimes won that race and stole
   ONE byte meant for our own trailing PowerShell text, corrupting it by
   exactly one dropped character (``SetThreadExecutionState`` read back
   as ``SetThreadExectionState``; ``uint32`` read back as ``unt32``) and
   completing the "wait" almost instantly instead of honouring the
   deadline. The failure mode is NOT an orphaned process — the stolen
   byte still made ``WaitAny`` return, so both release calls still ran
   and the process still exited (just ~1.7 hours early for a 2-hour
   ``max_hours=0.0006`` test, i.e. at ~0.5s instead of ~2.2s) — it is
   ``held()`` reporting a hold that has silently stopped holding
   anything. Wrapping the entire body as ``& { stmt1; stmt2; ...; }``
   forces the parser to consume the complete statement before executing
   any of it, so by the time the internal read fires there is nothing of
   our own script left in the pipe to steal. NOTE: the original design's
   blocking ``[Console]::In.ReadLine()`` had this exact same contention
   whenever ``shutdown`` was requested, since its release calls also
   follow the read — this was never sleep-only-safe either, it was just
   never measured with tracing precise enough to catch a race that a
   sleep-only script cannot exhibit.
7. **``reason`` is sanitized, never trusted, before it reaches the
   script.** Point 6 made the whole script ONE PowerShell statement, which
   means a newline (or any other control character) inside ``reason``
   is no longer cosmetic — it is a second top-level line the parser sees
   as separate from the statement above. Measured live 2026-08-13: an
   unsanitized ``reason`` containing ``\n`` produced a script that
   silently executed NOTHING at all when piped to the real host — alive
   at 12s against a 2.16s deadline, exit 0, zero stderr, only EOF ended
   it, i.e. ``held()`` reporting BOTH locks acquired with ZERO
   protection actually in place. This is the worst outcome the whole
   design can produce — reporting protection that does not exist — so it
   is fixed even though nothing in ``crr`` calls ``hold()`` with an
   attacker- or user-controlled ``reason`` today (the sole producer,
   ``crr/core/power.py``, builds a fixed ``"crr: N Claude session(s)
   live"`` string). ``reason`` is cosmetic display text for the OS's
   blocking UI, so a bad ``reason`` must degrade the MESSAGE, never the
   HOLD: ``_sanitize_reason`` strips every control character (0x00-0x1F,
   plus DEL) to a space, collapses the resulting whitespace runs, and
   only THEN doubles single quotes for the PowerShell string literal —
   never raises.

KNOWN LIMIT, recorded rather than assumed: registration returning TRUE
proves the reason REGISTERS, not that shutdown is BLOCKED. Microsoft is
explicit that an application without a visible window cannot cancel
shutdown. Making the window visible at the moment it matters is the tray
plan's job; this holder registers the reason and reports its efficacy as
unverified.

SECOND KNOWN LIMIT, same shape: ``held()`` is a LIVENESS poll, not proof of
acquisition. It reports the requested set whenever the child process is
still running, and nothing reads back from the child to confirm the two
API calls actually succeeded. If ``Add-Type`` fails under
``$ErrorActionPreference='Stop'`` -- a missing assembly, a constrained
language mode, an AppLocker policy -- the ``& { ... }`` statement aborts
before either lock is taken, yet the PowerShell host process stays alive
reading stdin, so ``held()`` reports BOTH locks while ZERO protection is
in place. Closing this would need the child to write an acquisition
receipt back over stdout and ``held()`` to require it. Until then the
limit is recorded rather than assumed, which is what makes it shippable:
a consumer must treat ``held()`` as "the holder is running", not as "the
machine is protected".
"""

from __future__ import annotations

import re
import subprocess

from crr.adapters._proc import (FORCE_WAIT_SECONDS, RELEASE_WAIT_SECONDS,
                                release_child)
from crr.core.config import DEFAULTS

_ES_CONTINUOUS = "0x80000000"
_ES_SYSTEM_REQUIRED = "0x00000001"
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED, precomputed and embedded as a single
# literal so the script text carries the combined flag value directly
# rather than an -bor expression PowerShell evaluates at runtime. Same
# numeric value either way; this form is what the test (and a reader
# grepping the emitted script) can see without evaluating PowerShell.
_ES_SLEEP_FLAGS = "0x80000001"

# Task.Wait(int millisecondsTimeout) takes a signed Int32. A caller passing
# an absurd max_hours must not overflow into a negative value -- .NET
# either rejects a negative timeout outright or, for -1 specifically,
# treats it as "wait forever" (the exact opposite of a backstop). Clamp
# defensively at both ends.
_INT32_MAX_MS = 2147483647

# Teardown budget. Measured normal teardown is ~2.07s (stdin close -> EOF ->
# both release calls -> [Environment]::Exit(0)), so blowing a 10s wait means
# something is genuinely wrong and escalating beats swallowing it: the
# process may still hold ShutdownBlockReasonCreate. Shared with the Linux
# holder via crr.adapters._proc (RELEASE_WAIT_SECONDS / FORCE_WAIT_SECONDS)
# so the two platforms cannot drift to different patience.

# Control characters (0x00-0x1F, plus DEL) that must never reach the
# emitted script unescaped: a newline or carriage return in `reason`
# breaks the one-PowerShell-statement invariant (module docstring, point
# 6) by adding a top-level line the parser will not see as part of the
# same statement. Measured live 2026-08-13: with a raw `\n` in `reason`,
# the emitted 3-line script silently executed NOTHING when piped to the
# real host -- alive well past its deadline, exit 0, zero stderr, only
# EOF ended it. held() would report both locks acquired while nothing
# was actually held. `reason` is cosmetic display text for the OS's
# blocking UI, so a bad reason must degrade the MESSAGE, never the HOLD.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _sanitize_reason(reason: str) -> str:
    """Make ``reason`` safe to embed in the ONE-line script, without ever
    raising: strip control characters (collapsing the whitespace they
    leave behind), THEN double any single quotes for the PowerShell
    string literal. Order matters -- quote-doubling first would not help
    a newline, and control-stripping after quote-doubling could still
    leave a stray control character sitting next to a doubled quote.
    """
    no_control = _CONTROL_CHARS_RE.sub(" ", reason)
    collapsed = _WHITESPACE_RUN_RE.sub(" ", no_control).strip()
    return collapsed.replace("'", "''")


def _timeout_ms(max_hours: float) -> int:
    """``max_hours`` in milliseconds, clamped to fit a signed 32-bit int.

    ``round()``, not ``int()`` truncation: ``0.0006 * 3600 * 1000`` is
    ``2159.9999999999995`` as a float, and truncating silently emits a
    literal (``2159``) that does not match the arithmetic it was derived
    from -- exactly the kind of unrecorded drift this codebase's
    provenance rules exist to catch, even though a 1ms error is harmless
    at the values this is actually called with.
    """
    ms = round(max_hours * 3600 * 1000)
    return max(0, min(ms, _INT32_MAX_MS))


def holder_argv() -> list[str]:
    """PowerShell, with stdin left open as the liveness channel."""
    return ["powershell.exe", "-NoProfile", "-Command", "-"]


def _ps_oneline_string(csharp_lines: list[str]) -> str:
    """A SINGLE PowerShell source line whose STRING VALUE holds ``\\n``-
    joined C# lines.

    Not a here-string (``@" ... "@``): measured live on 2026-08-13, that
    construct silently executes nothing at all when piped to
    ``powershell.exe -Command -`` over a non-console stdin (see the module
    docstring, point 4). Embedded ``"`` is backtick-escaped and line
    breaks are PowerShell's own ``` `n ``` escape, so the *value* is
    multi-line C# while the *source* stays one line.
    """
    escaped = (line.replace('"', '`"') for line in csharp_lines)
    return '"' + "`n".join(escaped) + '"'


def holder_script(want: frozenset[str], reason: str,
                  max_hours: float = DEFAULTS["power_block_max_hours"]) -> str:
    """The PowerShell program that holds the locks until stdin closes.

    The body is emitted as ONE PowerShell statement -- see point 6 in the
    module docstring for why: our own internal stdin read and
    ``-Command -``'s own read of "the rest of the script" pull from the
    SAME kernel pipe, and any of our script left unparsed when our read
    fires is exactly what that read can steal a byte from.
    """
    safe = _sanitize_reason(reason)
    # Only declare the P/Invoke signatures for what `want` actually calls.
    # Declaring ShutdownBlockReasonCreate/Destroy on a sleep-only hold
    # would be harmless at runtime but makes the emitted script lie about
    # what the process is going to do -- and a sleep-only script that
    # mentions ShutdownBlockReasonCreate is indistinguishable, on
    # inspection, from one that registers a block it doesn't release.
    sig_lines: list[str] = []
    if "sleep" in want:
        sig_lines += [
            '[DllImport("kernel32.dll", SetLastError=true)]',
            "public static extern uint SetThreadExecutionState(uint esFlags);",
        ]
    if "shutdown" in want:
        sig_lines += [
            '[DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)]',
            "public static extern bool ShutdownBlockReasonCreate(IntPtr hWnd, string pwszReason);",
            '[DllImport("user32.dll", SetLastError=true)]',
            "public static extern bool ShutdownBlockReasonDestroy(IntPtr hWnd);",
        ]
    stmts = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Windows.Forms",
        f"$sig = {_ps_oneline_string(sig_lines)}",
        "$api = Add-Type -MemberDefinition $sig -Name CrrHold "
        "-Namespace CrrPower -PassThru",
    ]
    if "sleep" in want:
        stmts.append(
            f"$null = $api::SetThreadExecutionState([uint32]'{_ES_SLEEP_FLAGS}')"
        )
    if "shutdown" in want:
        stmts += [
            "$form = New-Object System.Windows.Forms.Form",
            "$handle = $form.Handle",
            f"$null = $api::ShutdownBlockReasonCreate($handle, '{safe}')",
        ]
    timeout_ms = _timeout_ms(max_hours)
    # THE orphan defence: ONE asynchronous read on the RAW stdin stream,
    # bounded by the full deadline. WaitAny returns -1 on timeout (the
    # WSL parent is presumably alive but max_hours elapsed; the pending
    # read is abandoned harmlessly since the process exits right after)
    # or the completed index on EOF (the WSL parent died and closed the
    # pipe) -- either way execution falls through to the release
    # statements below.
    #
    # Deliberately NOT `while (deadline) { ReadLine() }`: ReadLine()
    # blocks synchronously, so a loop around it only re-checks the
    # deadline between completed reads and never re-fires while blocked
    # in the one and only read.
    #
    # Deliberately NOT [Console]::In (ReadLine or ReadLineAsync):
    # [Console]::In wraps a SyncTextReader, and ReadLineAsync() on it
    # runs synchronously on the calling thread despite returning a Task
    # -- measured live, the assignment itself did not return for 30+
    # seconds with stdin open and no data, so a .Wait(ms) after it never
    # got a chance to time out. OpenStandardInput() returns the raw,
    # unwrapped Stream, whose ReadAsync() is genuinely async.
    #
    # Do not re-issue a read in a loop -- multiple pending reads on the
    # same stream is its own bug.
    stmts += [
        "$stdin = [Console]::OpenStandardInput()",
        "$buf = New-Object byte[] 1",
        "$readTask = $stdin.ReadAsync($buf, 0, 1)",
        "$null = [System.Threading.Tasks.Task]::WaitAny("
        f"@($readTask), [int]{timeout_ms})",
    ]
    if "shutdown" in want:
        stmts.append("$null = $api::ShutdownBlockReasonDestroy($handle)")
    if "sleep" in want:
        stmts.append(
            f"$null = $api::SetThreadExecutionState([uint32]'{_ES_CONTINUOUS}')"
        )
    # [Environment]::Exit(0), not a bare `exit`: `powershell.exe
    # -Command -` behaves like an interactive session and does not quit
    # when it runs out of piped script -- it goes back to reading stdin
    # for the NEXT command. Releasing the locks above is necessary but
    # not sufficient for "the orphan is gone"; this is what actually
    # ends the process once the deadline fires or EOF arrives.
    stmts.append("[Environment]::Exit(0)")
    # A leading comment line, OUTSIDE the statement below, documents
    # max_hours for a human reading the emitted script. It cannot go
    # INSIDE the "; "-joined statement: `#` comments to end-of-line, and
    # since the statement is deliberately kept on ONE line (see the
    # docstring above), an inline comment would silently comment out
    # every statement after it -- including the release calls and the
    # exit.
    body = "; ".join(stmts)
    return f"# max_hours={max_hours}\n& {{ {body} }}\n"


class WindowsPowerHolder:
    # max_hours defaults to the versioned config prior, never a repeated
    # literal: `power_block_max_hours` is already a named key, and a second
    # copy here is a second answer to the same question.
    def __init__(self, spawn=None,
                 max_hours: float = DEFAULTS["power_block_max_hours"]) -> None:
        self._spawn = spawn or (lambda argv, **kw: subprocess.Popen(argv, **kw))
        self._max_hours = max_hours
        self._proc = None
        self._held: frozenset[str] = frozenset()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"sleep", "shutdown"})

    def hold(self, want: frozenset[str], reason: str) -> None:
        effective = frozenset(want) & self.capabilities()
        if effective == self._held and self._alive():
            return
        self.release()
        if not effective:
            return
        proc = self._spawn(
            holder_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Tracked BEFORE the write, not after. A BrokenPipeError on the
        # write would otherwise leave a spawned child with no handle to it
        # -- and a Windows interop child does not die with its WSL parent,
        # so "untracked" means "unkillable".
        self._proc = proc
        script = holder_script(effective, reason, max_hours=self._max_hours)
        if getattr(proc, "stdin", None) is not None:
            proc.stdin.write(script)
            proc.stdin.flush()
            # Deliberately NOT closed: the open pipe is the liveness
            # signal. Closing it here would make the holder exit at once.
        # Claimed only once the script actually reached the child: a
        # process that never received the script holds nothing.
        self._held = effective

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def held(self) -> frozenset[str]:
        return self._held if self._alive() else frozenset()

    def release(self) -> None:
        """Close stdin, then escalate until the child is CONFIRMED reaped.

        The handle is dropped only on confirmation. The old version fired
        terminate() and then cleared ``_proc``/``_held`` unconditionally
        with no re-wait: crr was left with no handle to a PowerShell that
        may still hold ``ShutdownBlockReasonCreate``, permanently
        uncleanable, ``held()`` reporting nothing held, and the user facing
        a machine that refuses to restart with nothing left to explain why.

        The escalation ladder itself (wait -> terminate+wait -> kill+wait)
        lives once, shared with the Linux holder, in
        ``crr.adapters._proc.release_child`` -- this method's own job is
        only the Windows-specific graceful step: closing stdin, which is
        the EOF signal the script's own async read is waiting on, so it
        must happen before the ladder's first wait or that wait is just
        waiting on nothing.

        Deliberately no ``finally:`` around the bookkeeping -- that is
        exactly the shape that reinstates the bug.
        """
        proc = self._proc
        if proc is None:
            self._held = frozenset()
            return
        try:
            if getattr(proc, "stdin", None) is not None:
                proc.stdin.close()             # EOF -> the script unwinds
        except Exception:
            pass
        if not release_child(proc, RELEASE_WAIT_SECONDS, FORCE_WAIT_SECONDS):
            # Neither the graceful EOF, terminate, nor kill confirmed it
            # dead. KEEP the handle and KEEP reporting the set: a live
            # child may genuinely still be holding, and the next hold()
            # retries release(). Reporting an empty hold here would be
            # the same lie, just quieter.
            return
        self._proc = None
        self._held = frozenset()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -v && .venv/bin/lint-imports`
Expected: all pass, contract kept

- [ ] **Step 5: Verify the real holder on this WSL host**

This is the one adapter that can be exercised end to end here. Run:

```bash
.venv/bin/python - <<'PY'
import time
from crr.adapters.power_hold_windows import WindowsPowerHolder
h = WindowsPowerHolder()
h.hold(frozenset({"sleep"}), "crr plan verification — transient")
print("held:", h.held())
time.sleep(2)
h.release()
print("after release:", h.held())
PY
```

Expected: `held: frozenset({'sleep'})` then `after release: frozenset()`,
with no PowerShell left behind (`pgrep -af powershell.exe` prints nothing
from this run). If a process survives, STOP: the orphan defence is broken
and that is the one defect in this plan that can leave the user's machine
unable to restart.

- [ ] **Step 6: Commit**

```bash
git add crr/adapters/power_hold_windows.py tests/test_power_adapters.py
git commit -m "feat(adapters): Windows hold, both locks in one child that dies on stdin EOF"
```

---

### Task 9: Selection — WSL takes the Windows holder

**Files:**
- Modify: `crr/cli.py`
- Test: `tests/test_power_adapters.py`

**Interfaces:**
- Consumes: all three holders (Tasks 6–8), `host.is_wsl` from `crr/adapters/host.py`
- Produces: `cli._power_holder(system: str, wsl: bool)` returning a `PowerHolder`; `cli._power_source(system: str, timeout: float)` returning a `PowerSource`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power_adapters.py`:

```python
from crr import cli as _cli
from crr.adapters.power_hold_linux import LinuxPowerHolder as _L
from crr.adapters.power_hold_macos import MacPowerHolder as _M
from crr.adapters.power_hold_windows import WindowsPowerHolder as _W


def test_wsl_selects_the_windows_holder_despite_reporting_linux():
    # platform.system() is "Linux" on WSL, so the obvious detect()-shaped
    # selection picks systemd-inhibit — which runs INSIDE the VM and
    # cannot affect the Windows host's power state at all. It would hold
    # successfully and protect nothing.
    assert isinstance(_cli._power_holder("Linux", wsl=True), _W)


def test_native_linux_selects_systemd_inhibit():
    assert isinstance(_cli._power_holder("Linux", wsl=False), _L)


def test_macos_selects_caffeinate():
    assert isinstance(_cli._power_holder("Darwin", wsl=False), _M)


def test_an_unsupported_platform_raises_rather_than_pretending():
    import pytest
    with pytest.raises(NotImplementedError) as e:
        _cli._power_holder("Plan9", wsl=False)
    assert "Plan9" in str(e.value)


def test_power_source_uses_sysfs_on_wsl_because_the_host_battery_is_exposed():
    from crr.adapters.power_source import SysfsPowerSource
    assert isinstance(_cli._power_source("Linux", 5.0), SysfsPowerSource)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_adapters.py -k "selects or unsupported_platform" -v`
Expected: FAIL with `AttributeError: module 'crr.cli' has no attribute '_power_holder'`

- [ ] **Step 3: Write minimal implementation**

Add to `crr/cli.py`'s adapter imports:

```python
from crr.adapters import (power_hold_linux, power_hold_macos,
                          power_hold_windows, power_source)
```

Add near the other selection helpers (e.g. after `_resolve_service_bin`):

```python
def _power_holder(system: str, wsl: bool):
    """The PowerHolder for this host.

    WSL is checked FIRST and deliberately. `platform.system()` returns
    "Linux" there, so the obvious detect()-shaped selection would pick
    systemd-inhibit — which runs inside the VM and cannot touch the
    Windows host's power state. It would hold successfully, report
    success, and protect nothing.
    """
    if wsl:
        return power_hold_windows.WindowsPowerHolder()
    if system == "Linux":
        return power_hold_linux.LinuxPowerHolder()
    if system == "Darwin":
        return power_hold_macos.MacPowerHolder()
    if system == "Windows":
        return power_hold_windows.WindowsPowerHolder()
    raise NotImplementedError(f"no power-hold adapter for {system!r} yet")


def _power_source(system: str, timeout: float):
    """The PowerSource for this host.

    Unlike the HOLD, WSL needs no interop here: WSL2 passes the Windows
    host's battery through sysfs, measured 2026-08-12.
    """
    if system == "Darwin":
        return power_source.MacPowerSource(timeout)
    return power_source.SysfsPowerSource()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_power_adapters.py
git commit -m "feat(cli): select the power holder, WSL before platform.system()"
```

---

## Self-Review

**Spec coverage.** Capability matrix → Tasks 6–8 (`capabilities()` per
platform). Lid rule → Task 6 tests. `sleep` not `idle` → Task 6. Tri-state
AC → Tasks 2, 5. WSL selection → Task 9. Orphan defence → Task 8. Config →
Task 1. `decide`/`unmet` → Tasks 2–3.

**Known gaps, deliberately deferred to the next plans in this phase** (each
is called out in the spec and none blocks this plan from shipping working
software):

- `crr-awake` unit and the poll loop — **next plan**, because it is the
  consumer of everything here and the unit-generation work belongs with it.
- `crr power` status/release command and doctor lines — **next plan**, with
  the unit.
- Windows tray + `WM_QUERYENDSESSION` dialog — separate plan.
- `crr harden` (Windows Update policy) — separate plan.

**Placeholder scan.** No TBD/TODO; every code step carries real code; every
test step carries real assertions.

**Type consistency.** `hold(want, reason)`, `held()`, `release()`,
`capabilities()` are identical across all three holders and match the
Protocol in Task 4. `on_ac()` returns `bool | None` in both sources and is
consumed as tri-state in `decide()`. `Decision.want` and `MODES` values are
both `frozenset[str]` over `{"sleep", "shutdown"}`, matching
`power_block`'s `"sleep" | "sleep+shutdown"` vocabulary from Task 1.
