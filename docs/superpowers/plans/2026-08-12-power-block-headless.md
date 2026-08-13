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
        # No power-supply devices at all. That is a desktop or a server —
        # a KNOWN mains machine, not an unknown one. Returning None here
        # would withhold the hold on every non-laptop.
        if not any(_read(e / "type") for e in entries):
            return True
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
- Produces: `LinuxPowerHolder(logind_conf: Path | None = None, spawn=subprocess.Popen)` with `.capabilities()`, `.hold(want, reason)`, `.release()`, `.held()`; module functions `inhibit_argv(want, reason) -> list[str]` and `lid_is_exempt(conf_text: str) -> bool`

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


def test_holder_refuses_to_block_sleep_when_the_lid_is_not_exempt(tmp_path):
    # The builder's hard requirement is that closing the lid always
    # sleeps. On a host that has turned the default off, a sleep lock
    # would break that, so crr withholds instead.
    conf = tmp_path / "logind.conf"
    conf.write_text("LidSwitchIgnoreInhibited=no\n", encoding="utf-8")
    spawned = []
    holder = LinuxPowerHolder(logind_conf=conf,
                              spawn=lambda argv, **kw: spawned.append(argv))
    holder.hold(frozenset({"sleep"}), "r")
    assert spawned == [], "blocked sleep on a host where that blocks the lid"
    assert holder.held() == frozenset()


def test_holder_still_blocks_shutdown_when_the_lid_is_not_exempt(tmp_path):
    # Only the sleep half is unsafe there; shutdown is unaffected by lid
    # handling, so withholding it too would be over-correction.
    conf = tmp_path / "logind.conf"
    conf.write_text("LidSwitchIgnoreInhibited=no\n", encoding="utf-8")
    spawned = []

    class _P:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    def _spawn(argv, **kw):
        spawned.append(argv)
        return _P()

    holder = LinuxPowerHolder(logind_conf=conf, spawn=_spawn)
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"shutdown"})
    what = spawned[0][spawned[0].index("--what") + 1]
    assert what == "shutdown"


def test_capabilities_are_both_on_linux(tmp_path):
    holder = LinuxPowerHolder(logind_conf=tmp_path / "absent.conf")
    assert holder.capabilities() == frozenset({"sleep", "shutdown"})
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
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

LOGIND_CONF = Path("/etc/systemd/logind.conf")

_LID_RE = re.compile(
    r"^\s*LidSwitchIgnoreInhibited\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE
)
_FALSEY = ("no", "false", "0", "off")


def lid_is_exempt(conf_text: str) -> bool:
    """True when closing the lid sleeps even while an inhibitor is held.

    Defaults to True because logind's own default is ``yes``; a commented
    line is not a setting, so the regex deliberately anchors on a line that
    does not start with ``#``.
    """
    match = None
    for candidate in _LID_RE.finditer(conf_text):
        line = conf_text[:candidate.start()].split("\n")[-1]
        if line.lstrip().startswith("#"):
            continue
        match = candidate
    if match is None:
        return True
    return match.group(1).strip().lower() not in _FALSEY


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

    def __init__(self, logind_conf: Path | None = None, spawn=None) -> None:
        self._conf = LOGIND_CONF if logind_conf is None else Path(logind_conf)
        self._spawn = spawn or (lambda argv, **kw: subprocess.Popen(argv, **kw))
        self._proc = None
        self._held: frozenset[str] = frozenset()
        self._withheld: str | None = None

    def capabilities(self) -> frozenset[str]:
        return frozenset({"sleep", "shutdown"})

    def withheld(self) -> str | None:
        """Why part of the request was dropped, for doctor."""
        return self._withheld

    def _lid_exempt(self) -> bool:
        try:
            text = self._conf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True  # logind's own default
        return lid_is_exempt(text)

    def hold(self, want: frozenset[str], reason: str) -> None:
        self._withheld = None
        effective = set(want)
        if "sleep" in effective and not self._lid_exempt():
            effective.discard("sleep")
            self._withheld = (
                "not blocking sleep: this host sets "
                "LidSwitchIgnoreInhibited=no, so a sleep inhibitor would "
                "also block closing the lid"
            )
        effective_fs = frozenset(effective)
        if effective_fs == self._held and self._alive():
            return
        self.release()
        if not effective_fs:
            return
        self._proc = self._spawn(
            inhibit_argv(effective_fs, reason),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._held = effective_fs

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
- Produces: `WindowsPowerHolder(spawn=None)` with the four methods; `holder_script(want: frozenset[str], reason: str) -> str`; `holder_argv() -> list[str]`

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
    s = holder_script(frozenset({"sleep"}), "r")
    assert "ReadLine" in s, "no stdin-EOF loop: an orphan would hold forever"


def test_script_self_releases_after_the_cap():
    s = holder_script(frozenset({"sleep"}), "r", max_hours=12)
    assert "12" in s


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

Create `crr/adapters/power_hold_windows.py`:

```python
"""Windows power hold from WSL, via one PowerShell child.

Both locks live in ONE process so there is one lifetime to reason about:
``SetThreadExecutionState`` for idle sleep, and a window's
``ShutdownBlockReasonCreate`` for restart. Both were measured callable
UNELEVATED from WSL on 2026-08-12.

Two things this file must never lose:

1. **The stdin-EOF exit.** A Windows interop child does NOT die with its
   WSL parent. Without this loop, a killed crr leaves a PowerShell holding
   a shutdown block forever — a machine that refuses to restart with
   nothing left running to explain why. That is the exact class of
   unexplained behaviour crr exists to eliminate, so the mechanism (not a
   timer) is the primary defence.
2. **``ES_SYSTEM_REQUIRED`` only, never ``ES_DISPLAY_REQUIRED``.** The lid
   must keep working and the screen must be allowed to turn off.

KNOWN LIMIT, recorded rather than assumed: registration returning TRUE
proves the reason REGISTERS, not that shutdown is BLOCKED. Microsoft is
explicit that an application without a visible window cannot cancel
shutdown. Making the window visible at the moment it matters is the tray
plan's job; this holder registers the reason and reports its efficacy as
unverified.
"""

from __future__ import annotations

import subprocess

_ES_CONTINUOUS = "0x80000000"
_ES_SYSTEM_REQUIRED = "0x00000001"


def holder_argv() -> list[str]:
    """PowerShell, with stdin left open as the liveness channel."""
    return ["powershell.exe", "-NoProfile", "-Command", "-"]


def holder_script(want: frozenset[str], reason: str,
                  max_hours: int = 12) -> str:
    """The PowerShell program that holds the locks until stdin closes."""
    safe = reason.replace("'", "''")
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Windows.Forms",
        "$sig = @\"",
        '[DllImport("kernel32.dll", SetLastError=true)]',
        "public static extern uint SetThreadExecutionState(uint esFlags);",
        '[DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)]',
        "public static extern bool ShutdownBlockReasonCreate(IntPtr hWnd, string pwszReason);",
        '[DllImport("user32.dll", SetLastError=true)]',
        "public static extern bool ShutdownBlockReasonDestroy(IntPtr hWnd);",
        '"@',
        "$api = Add-Type -MemberDefinition $sig -Name CrrHold "
        "-Namespace CrrPower -PassThru",
    ]
    if "sleep" in want:
        lines.append(
            f"$null = $api::SetThreadExecutionState("
            f"[uint32]'{_ES_CONTINUOUS}' -bor [uint32]'{_ES_SYSTEM_REQUIRED}')"
        )
    if "shutdown" in want:
        lines += [
            "$form = New-Object System.Windows.Forms.Form",
            "$handle = $form.Handle",
            f"$null = $api::ShutdownBlockReasonCreate($handle, '{safe}')",
        ]
    lines += [
        # THE orphan defence: when the WSL parent dies the pipe closes,
        # ReadLine returns $null, and every lock is released below.
        f"$deadline = (Get-Date).AddHours({max_hours})",
        "while ((Get-Date) -lt $deadline) {",
        "  $line = [Console]::In.ReadLine()",
        "  if ($line -eq $null) { break }",
        "}",
    ]
    if "shutdown" in want:
        lines.append("$null = $api::ShutdownBlockReasonDestroy($handle)")
    if "sleep" in want:
        lines.append(
            f"$null = $api::SetThreadExecutionState([uint32]'{_ES_CONTINUOUS}')"
        )
    return "\n".join(lines) + "\n"


class WindowsPowerHolder:
    def __init__(self, spawn=None, max_hours: int = 12) -> None:
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
        script = holder_script(effective, reason, max_hours=self._max_hours)
        if getattr(proc, "stdin", None) is not None:
            proc.stdin.write(script)
            proc.stdin.flush()
            # Deliberately NOT closed: the open pipe is the liveness
            # signal. Closing it here would make the holder exit at once.
        self._proc = proc
        self._held = effective

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def held(self) -> frozenset[str]:
        return self._held if self._alive() else frozenset()

    def release(self) -> None:
        if self._proc is not None:
            try:
                if getattr(self._proc, "stdin", None) is not None:
                    self._proc.stdin.close()   # EOF -> the script unwinds
                self._proc.wait(timeout=10)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
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
