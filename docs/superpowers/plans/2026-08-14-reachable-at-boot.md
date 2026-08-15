# reachable-at-boot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `crr reachable-at-boot` — bring crr's control surface (dashboard +
reviver) up at boot without an interactive login, so a reboot is a survivable
non-event, and **measure** whether a real reboot actually came up headless.

**Architecture:** A pure core turns three boot timestamps (machine boot,
control-surface boot, earliest login) into a `headless / login_only / unknown`
verdict. Per-platform adapters read those timestamps and (WSL) generate the two
Scheduled Tasks that were validated by hand on the real host. `crr reachable-at-boot`
reports by default, installs only with `--install` behind a tty confirmation +
UAC, and `crr doctor` renders the verdict. Selection branches on `host.is_wsl()`
before `platform.system()`.

**Tech Stack:** Python 3.11+ stdlib only. Windows Scheduled Tasks via
`powershell.exe`/interop; systemd `loginctl`; launchd plists. pytest, import-linter.

## Global Constraints

- **Runtime deps stay at zero.** Adding to `pyproject.toml` runtime deps is a
  design regression (AGENTS.md).
- **One-way layering, machine-enforced:** `crr.cli` → `crr.adapters` →
  `crr.core`. `crr.core` imports neither adapters nor cli.
- **Test-first.** Every behaviour gets a failing test before implementation.
- **Every judgment-call constant is a named config key** in `crr/core/config.py`
  `DEFAULTS` with a `CONFIG_DEFAULTS_VERSION` bump (currently **18**) — never a
  literal in logic (`tests/test_priors.py`).
- **Null results stay null.** A missing/unreadable timestamp yields `unknown`,
  never `headless` and never `login_only`. "reachable" is never rendered as
  "an unlocked desktop".
- **crr must never claim more than it verified.** The output distinguishes
  *installed a task* from *confirmed a reboot came up headless*.
- **⛔ NO TEST MAY REGISTER A REAL SCHEDULED TASK, WRITE HKLM, OR REBOOT.**
  Task text is asserted as strings; registration is exercised with an injected
  runner. A subagent on an earlier plan really created a Windows Scheduled Task
  on this host — do not repeat that shape. If you think you need a real
  registration to verify, STOP and report.
- Run before every commit: `.venv/bin/python -m pytest tests/ -q` and
  `.venv/bin/lint-imports`. The pre-commit hook runs both.
- Use `.venv/bin/python`, never bare `python`. `crr` on PATH is the deployed
  copy; use `.venv/bin/crr` for working-tree changes.
- Spec: `docs/superpowers/specs/2026-08-14-boot-at-startup-design.md`.

## Measured facts about this host (fixtures — do not re-derive by writing)

From the proven manual test on 2026-08-14:
- distro `Ubuntu-24.04`, Windows user `Infin`, Linux user `evan`, preferred
  tailnet `infiniteinsight@gmail.com`.
- Windows booted 16:42:29; WSL/systemd came up 16:43:08 (**39s later**); no
  interactive login; `LogonUI.exe` running (desktop locked); `AutoAdminLogon`
  not set. So `crr reachable-at-boot` on this host must currently verdict
  **headless** (the two tasks are already installed by hand).
- linger is already enabled (`loginctl show-user evan Linger=yes`); `crr-web` /
  `crr-revive.timer` are enabled user units.

## File Structure

| File | Responsibility |
|---|---|
| `crr/core/boot_survival.py` (create) | Pure verdict from timestamps; per-platform "what's needed" |
| `crr/core/config.py` (modify) | 2 keys + version 18→19 |
| `crr/adapters/boot_windows.py` (create) | Generate the 2 tasks (text); read Windows/WSL boot + login facts |
| `crr/adapters/boot_linux.py` (create) | linger state; crr-web ActiveEnterTimestamp; boot + login facts |
| `crr/adapters/boot_macos.py` (create) | LaunchDaemon plist (unverified); FileVault detection |
| `crr/cli.py` (modify) | `reachable-at-boot [--install|--uninstall]`; selection; doctor line |
| `tests/test_boot_survival.py` (create) | Core verdict tests |
| `tests/test_boot_adapters.py` (create) | Adapter tests |

Out of scope: SSH-into-WSL; verifying macOS on real hardware (#43).

---

### Task 1: Config priors

**Files:**
- Modify: `crr/core/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `DEFAULTS["boot_headless_window_seconds"]` (int, 300),
  `DEFAULTS["boot_preferred_tailnet"]` (str, ""); `CONFIG_DEFAULTS_VERSION == 19`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_reachable_at_boot_keys_exist():
    # A restart-came-up-headless window: WSL/systemd started within this many
    # seconds of MACHINE boot counts as headless (vs. only at login). 5 min is
    # generous slack for a slow cold boot; measured real gap was 39s.
    assert cfg.DEFAULTS["boot_headless_window_seconds"] == 300
    # Empty = "the tailnet active at install time"; crr never silently picks.
    assert cfg.DEFAULTS["boot_preferred_tailnet"] == ""


def test_config_defaults_version_covers_the_boot_keys():
    assert cfg.CONFIG_DEFAULTS_VERSION >= 19
```

Update the exact-version assertion in
`test_vestigial_keys_are_gone_and_version_bumped` from 18 to 19.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -k reachable_at_boot -v`
Expected: FAIL with `KeyError: 'boot_headless_window_seconds'`

- [ ] **Step 3: Write minimal implementation**

In `crr/core/config.py`, add a `# v19:` entry at the END of the version-history
comment block (never edit an existing entry — read the `v9: SKIPPED` entry to
see why), bump the constant to 19, and append to `DEFAULTS`:

```python
    # reachable-at-boot (spec 2026-08-14). The window, in seconds, within which
    # the control surface coming up after MACHINE boot counts as "headless"
    # rather than "only at login". Generous slack for a slow cold boot; the
    # measured real gap on the reference host was 39s.
    "boot_headless_window_seconds": 300,
    # Which Tailscale account the boot task re-selects. Empty means "whatever
    # is active at install time" — crr never silently picks a tailnet.
    "boot_preferred_tailnet": "",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add crr/core/config.py tests/test_config.py
git commit -m "feat(config): reachable-at-boot priors — headless window + preferred tailnet"
```

---

### Task 2: Core verdict — did the surface come up headless?

**Files:**
- Create: `crr/core/boot_survival.py`
- Test: `tests/test_boot_survival.py`

**Interfaces:**
- Produces:
  - `BootVerdict(status: str, detail: str)` — frozen dataclass; `status` is one
    of `"headless"`, `"login_only"`, `"unknown"`
  - `interpret_boot(machine_boot: float | None, surface_boot: float | None, first_login: float | None, window_seconds: int) -> BootVerdict`

Timestamps are epoch seconds (float) or `None` when unreadable.

- [ ] **Step 1: Write the failing test**

Create `tests/test_boot_survival.py`:

```python
"""reachable-at-boot verdict (spec 2026-08-14) — pure, no I/O.

Given machine boot, control-surface boot, and earliest interactive login,
decide whether the surface came up headless (a reboot is survivable), only at
login (the boot task did not fire), or unknown (a timestamp was unreadable).
An unknown must never render as headless.
"""

import pytest

from crr.core.boot_survival import BootVerdict, interpret_boot

WIN = 300


def test_surface_up_seconds_after_boot_and_before_login_is_headless():
    # The reference host: machine boot t=0, surface up t=39, no login yet.
    v = interpret_boot(machine_boot=0.0, surface_boot=39.0, first_login=None,
                       window_seconds=WIN)
    assert v.status == "headless"


def test_surface_up_before_a_later_login_is_headless():
    v = interpret_boot(machine_boot=0.0, surface_boot=39.0, first_login=500.0,
                       window_seconds=WIN)
    assert v.status == "headless"


def test_surface_up_only_at_login_is_login_only():
    # Machine booted at 0, nobody around; surface didn't come up until the
    # login 8 hours later. The boot task did not fire.
    v = interpret_boot(machine_boot=0.0, surface_boot=28800.0,
                       first_login=28795.0, window_seconds=WIN)
    assert v.status == "login_only"


def test_just_outside_the_window_with_no_login_is_unknown_not_headless():
    # Came up 10 min after boot, but no login recorded to explain it. We cannot
    # claim headless (too late) nor login_only (no login). Unknown.
    v = interpret_boot(machine_boot=0.0, surface_boot=600.0, first_login=None,
                       window_seconds=WIN)
    assert v.status == "unknown"


@pytest.mark.parametrize("m,s,l", [
    (None, 39.0, None),
    (0.0, None, None),
    (None, None, None),
])
def test_a_missing_timestamp_is_unknown(m, s, l):
    assert interpret_boot(m, s, l, WIN).status == "unknown"


def test_verdict_is_frozen():
    import dataclasses
    v = interpret_boot(0.0, 39.0, None, WIN)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.status = "x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_boot_survival.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crr.core.boot_survival'`

- [ ] **Step 3: Write minimal implementation**

Create `crr/core/boot_survival.py`:

```python
"""Did crr's control surface come up at boot, headless? (spec 2026-08-14)

Pure: three timestamps in, a verdict out. No I/O, no platform. The adapters
read the clocks; this decides what they mean, and it keeps an unknown unknown
— a reboot reported "survivable" when it was not is the exact failure this
whole project is built against.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BootVerdict:
    status: str   # "headless" | "login_only" | "unknown"
    detail: str


def interpret_boot(
    machine_boot: float | None,
    surface_boot: float | None,
    first_login: float | None,
    window_seconds: int,
) -> BootVerdict:
    """Classify how the control surface came up after the last boot."""
    if machine_boot is None or surface_boot is None:
        return BootVerdict("unknown", "could not read the boot timestamps")
    gap = surface_boot - machine_boot
    came_up_before_login = first_login is None or surface_boot < first_login
    if gap <= window_seconds and came_up_before_login:
        return BootVerdict(
            "headless",
            f"the control surface came up {int(gap)}s after boot, "
            "before any login — a reboot is survivable",
        )
    if first_login is not None and surface_boot >= first_login:
        return BootVerdict(
            "login_only",
            "the control surface did not come up until login — the boot task "
            "did not fire; run `crr reachable-at-boot --install`",
        )
    return BootVerdict(
        "unknown",
        f"the control surface came up {int(gap)}s after boot with no login to "
        "explain it — cannot confirm it survives a reboot",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_boot_survival.py -v && .venv/bin/lint-imports`
Expected: 9 passed, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/core/boot_survival.py tests/test_boot_survival.py
git commit -m "feat(core): boot-survival verdict, unknowns stay unknown"
```

---

### Task 3: WSL — generate the two Scheduled Tasks (text only)

**Files:**
- Create: `crr/adapters/boot_windows.py`
- Test: `tests/test_boot_adapters.py`

**Interfaces:**
- Produces:
  - `wsl_boot_argument(distro: str, linux_user: str) -> str`
  - `tailnet_script(preferred_tailnet: str) -> str`
  - `install_script(distro: str, linux_user: str, tailnet: str | None, script_path: str) -> str`
  - `WSL_BOOT_TASK = "crr-wsl-boot"`, `TAILNET_TASK = "crr-tailnet-default"`

Generation only. Registration (Task 7) wraps `install_script` in an elevated
runner. **No test registers anything.**

- [ ] **Step 1: Write the failing test**

Create `tests/test_boot_adapters.py`:

```python
"""reachable-at-boot adapters — generation asserted as text; NO registration.

Nothing here registers a Scheduled Task, writes the registry, or reboots. The
elevated register path is exercised with an injected runner in the CLI tests.
"""

from crr.adapters.boot_windows import (TAILNET_TASK, WSL_BOOT_TASK,
                                       install_script, tailnet_script,
                                       wsl_boot_argument)


def test_wsl_boot_argument_holds_the_vm_open():
    arg = wsl_boot_argument("Ubuntu-24.04", "evan")
    # -d <distro> -u <user>, and the keepalive that defeats WSL's ~60s idle
    # shutdown (otherwise the VM boots and dies before you'd notice).
    assert "-d Ubuntu-24.04" in arg
    assert "-u evan" in arg
    assert "exec sleep infinity" in arg


def test_tailnet_script_retries_until_tailscaled_is_ready():
    s = tailnet_script("infiniteinsight@gmail.com")
    assert "switch infiniteinsight@gmail.com" in s
    # A plain switch at boot races tailscaled; the script must retry.
    assert "for" in s.lower() and "start-sleep" in s.lower()


def test_install_script_registers_both_tasks_with_the_right_shape():
    s = install_script("Ubuntu-24.04", "evan", "infiniteinsight@gmail.com",
                       r"C:\ProgramData\crr\tailnet-default.ps1")
    assert f"Register-ScheduledTask" in s
    assert WSL_BOOT_TASK in s and TAILNET_TASK in s
    # S4U: no stored password (PIN login), and it kept the desktop locked.
    assert "S4U" in s
    assert "AtStartup" in s
    assert "Highest" in s
    # unbounded so the sleep-infinity keepalive is not killed after 3 days
    assert "New-TimeSpan" in s or "[TimeSpan]::Zero" in s


def test_install_script_omits_the_tailnet_task_when_no_preference():
    # Single-account host: no tailnet task, no switch script.
    s = install_script("Ubuntu-24.04", "evan", None, r"C:\ProgramData\crr\x.ps1")
    assert WSL_BOOT_TASK in s
    assert TAILNET_TASK not in s
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -k "wsl_boot or tailnet or install_script" -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `crr/adapters/boot_windows.py`. Mirror the elevation discipline in
`crr/adapters/harden_windows.py::_elevated_reg_add` (the `-PassThru` +
`if (-not $p) { exit 1 }` + `exit $p.ExitCode` shape). The install writes the
tasks via a `.ps1` run elevated, so quoting is handled by a file, not nested
inline strings.

```python
"""Windows/WSL side of reachable-at-boot: generate the boot Scheduled Tasks,
and read the boot/login facts that prove whether they fired.

Validated by hand on the reference host: an AtStartup S4U task running
`wsl.exe ... exec sleep infinity` brought WSL + the dashboard up 39s after a
cold boot with no login and the desktop still locked. This generates that
reproducibly. NOTHING here registers a task; the cli runs the generated script
elevated after a confirmation.
"""

from __future__ import annotations

WSL_BOOT_TASK = "crr-wsl-boot"
TAILNET_TASK = "crr-tailnet-default"

_WSL = r"C:\Windows\System32\wsl.exe"


def wsl_boot_argument(distro: str, linux_user: str) -> str:
    """Args to wsl.exe that boot the distro and hold the VM open forever."""
    return f'-d {distro} -u {linux_user} -e sh -c "exec sleep infinity"'


def tailnet_script(preferred_tailnet: str) -> str:
    """PowerShell that re-selects the preferred tailnet, retrying until
    tailscaled is up (a plain switch at boot races the service)."""
    return (
        "$ts = 'C:\\Program Files\\Tailscale\\tailscale.exe'\n"
        "for ($i = 0; $i -lt 30; $i++) {\n"
        f"    & $ts switch {preferred_tailnet} 2>$null\n"
        "    if ($LASTEXITCODE -eq 0) { break }\n"
        "    Start-Sleep -Seconds 2\n"
        "}\n"
    )


def _register_block(task: str, execute: str, argument: str) -> str:
    # One AtStartup / S4U / Highest task, unbounded run time. -Force makes
    # re-install idempotent.
    return (
        f"$a = New-ScheduledTaskAction -Execute '{execute}' -Argument '{argument}'\n"
        "$t = New-ScheduledTaskTrigger -AtStartup\n"
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)\n"
        "$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U "
        "-RunLevel Highest\n"
        f"Register-ScheduledTask -TaskName '{task}' -Action $a -Trigger $t "
        "-Settings $s -Principal $p -Force | Out-Null\n"
    )


def install_script(distro: str, linux_user: str, tailnet: str | None,
                   script_path: str) -> str:
    """The full PowerShell the cli runs elevated to register the task(s)."""
    parts = ["$ErrorActionPreference = 'Stop'\n"]
    parts.append(_register_block(WSL_BOOT_TASK, _WSL,
                                 wsl_boot_argument(distro, linux_user)))
    if tailnet:
        # Write the retry script next to where the task will call it, then
        # register the task that runs it.
        safe = tailnet_script(tailnet).replace("'", "''")
        parts.append(f"$dir = Split-Path -Parent '{script_path}'\n")
        parts.append("New-Item -ItemType Directory -Force -Path $dir | Out-Null\n")
        parts.append(f"Set-Content -Path '{script_path}' -Value '{safe}' -Encoding ASCII\n")
        parts.append(_register_block(
            TAILNET_TASK, "powershell.exe",
            f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{script_path}\"'))
    return "".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -v && .venv/bin/lint-imports`
Expected: all pass, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/boot_windows.py tests/test_boot_adapters.py
git commit -m "feat(adapters): generate the WSL boot + tailnet tasks (text only, no registration)"
```

---

### Task 4: WSL — read the boot/login facts

**Files:**
- Modify: `crr/adapters/boot_windows.py`
- Test: `tests/test_boot_adapters.py`

**Interfaces:**
- Consumes: nothing from other tasks
- Produces:
  - `BootFacts(machine_boot: float | None, surface_boot: float | None, first_login: float | None, locked: bool | None, autologin: bool | None)` — frozen dataclass
  - `parse_epoch(text: str) -> float | None`
  - `read_facts(run=None) -> BootFacts`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_boot_adapters.py`:

```python
from crr.adapters.boot_windows import BootFacts, parse_epoch, read_facts


def test_parse_epoch_reads_a_numeric_line_and_rejects_junk():
    assert parse_epoch("1723668188\n") == 1723668188.0
    assert parse_epoch("") is None
    assert parse_epoch("not-a-number") is None


def test_read_facts_is_all_unknown_when_the_probe_fails():
    def boom(argv, timeout):
        raise OSError("powershell.exe not found")
    f = read_facts(run=boom)
    assert f == BootFacts(None, None, None, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -k "parse_epoch or read_facts" -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write minimal implementation**

Append to `crr/adapters/boot_windows.py`. Read `crr/adapters/diagnostics_windows.py`
for the `run_capture`-via-interop pattern. The single PowerShell prints five
lines (epoch seconds or `-` / `0` / `1`): Windows boot epoch, WSL boot epoch,
earliest login epoch, LogonUI-present flag, AutoAdminLogon flag. WSL boot epoch
is read from inside WSL (this process's `/proc/1` start or `journalctl`),
Windows facts via interop. Every read failure yields `None`. Follow the
tri-state discipline: absent/unreadable → `None`, never a guess.

Suggested shape (adjust to the codebase's helper names):

```python
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BootFacts:
    machine_boot: float | None
    surface_boot: float | None
    first_login: float | None
    locked: bool | None
    autologin: bool | None


def parse_epoch(text: str) -> float | None:
    line = text.strip().splitlines()[0] if text.strip() else ""
    try:
        return float(line)
    except ValueError:
        return None


def _wsl_boot_epoch(boot_stat: Path = Path("/proc/1")) -> float | None:
    # systemd (PID 1) start time == when this WSL instance booted.
    try:
        return boot_stat.stat().st_ctime
    except OSError:
        return None
```

`read_facts(run=None)` composes: `machine_boot`/`first_login`/`locked`/
`autologin` from one interop PowerShell call (parsed line-by-line, each `None`
on failure), and `surface_boot` from `_wsl_boot_epoch()`. On any exception from
`run`, return `BootFacts(None, None, None, None, None)`. Keep the PowerShell a
read-only `Get-*` command; assert in a test that it contains no `Set-`/`reg add`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -v && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Verify against this real host — READ ONLY**

Run: `.venv/bin/python -c "from crr.adapters.boot_windows import read_facts; print(read_facts())"`
Expected: `machine_boot` and `surface_boot` both populated, `surface_boot`
within a few minutes of `machine_boot`, `locked=True`, `autologin=False` —
matching the measured state. If `surface_boot` is 8h after `machine_boot`, the
boot task is not firing; report it. Do not write anything.

- [ ] **Step 6: Commit**

```bash
git add crr/adapters/boot_windows.py tests/test_boot_adapters.py
git commit -m "feat(adapters): read the WSL boot/login facts, unknowns stay None"
```

---

### Task 5: Linux — linger + surface-boot facts

**Files:**
- Create: `crr/adapters/boot_linux.py`
- Test: `tests/test_boot_adapters.py`

**Interfaces:**
- Produces:
  - `linger_enabled(user: str, run=None) -> bool | None`
  - `read_facts(user: str, run=None) -> BootFacts` (reuses `boot_windows.BootFacts`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_boot_adapters.py`:

```python
from crr.adapters import boot_linux


def test_linger_enabled_reads_loginctl():
    def fake(argv, timeout):
        assert "show-user" in argv
        return "Linger=yes\n"
    assert boot_linux.linger_enabled("evan", run=fake) is True

    assert boot_linux.linger_enabled(
        "evan", run=lambda a, timeout: "Linger=no\n") is False


def test_linger_unknown_when_loginctl_fails():
    def boom(argv, timeout):
        raise OSError("no loginctl")
    assert boot_linux.linger_enabled("evan", run=boom) is None


def test_linux_read_facts_all_none_on_failure():
    def boom(argv, timeout):
        raise OSError("x")
    f = boot_linux.read_facts("evan", run=boom)
    assert f.machine_boot is None and f.surface_boot is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -k "linger or linux_read" -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `crr/adapters/boot_linux.py`. On native Linux the control surface already
comes up at boot via linger; this adapter's job is to confirm and to supply the
verdict's timestamps.
- `linger_enabled(user, run)` parses `loginctl show-user <user> --property=Linger`
  → `True`/`False`; any failure → `None`.
- `read_facts(user, run)`:
  - `machine_boot`: system boot epoch (`/proc/stat` `btime`, or
    `stat /proc/1`).
  - `surface_boot`: `crr-web.service` user-unit `ActiveEnterTimestampMonotonic`
    resolved to epoch, or the unit's `ActiveEnterTimestamp` parsed to epoch via
    `systemctl --user show crr-web.service -p ActiveEnterTimestamp`.
  - `first_login`: earliest entry from `last -F` / `who`, epoch, or `None`.
  - `locked`/`autologin`: `None` (not meaningful on Linux; the verdict does not
    require them).
  Any exception → all-`None` `BootFacts`.

Import `BootFacts` from `crr.adapters.boot_windows` (adapter→adapter is allowed;
`lint-imports` only forbids adapters→cli and core→adapters).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -v && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/boot_linux.py tests/test_boot_adapters.py
git commit -m "feat(adapters): linux linger + surface-boot facts"
```

---

### Task 6: macOS — LaunchDaemon plist + FileVault (unverified)

**Files:**
- Create: `crr/adapters/boot_macos.py`
- Test: `tests/test_boot_adapters.py`

**Interfaces:**
- Produces:
  - `web_daemon_plist(crr_bin: str, path: str, port: int) -> str`
  - `filevault_enabled(run=None) -> bool | None`
  - `DAEMON_LABEL = "com.claude-remote-rescue.web-daemon"`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_boot_adapters.py`:

```python
import plistlib

from crr.adapters import boot_macos


def test_web_daemon_is_a_boot_daemon_not_a_login_agent():
    parsed = plistlib.loads(
        boot_macos.web_daemon_plist("/opt/crr/bin/crr", "/usr/bin", 8765).encode())
    assert parsed["Label"] == boot_macos.DAEMON_LABEL
    # RunAtLoad + KeepAlive so it starts at boot, before login — the whole
    # point vs. crr's existing LaunchAgents which need a GUI session.
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["ProgramArguments"][:2] == ["/opt/crr/bin/crr", "web"]


def test_filevault_parsing():
    assert boot_macos.filevault_enabled(
        run=lambda a, timeout: "FileVault is On.\n") is True
    assert boot_macos.filevault_enabled(
        run=lambda a, timeout: "FileVault is Off.\n") is False
    assert boot_macos.filevault_enabled(
        run=lambda a, timeout: "???\n") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -k "web_daemon or filevault" -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `crr/adapters/boot_macos.py`. Mirror `crr/adapters/launchd.py`'s plist
building (`plistlib.dumps`), but a **LaunchDaemon** (root, `/Library/LaunchDaemons`)
with `RunAtLoad`/`KeepAlive` true. `filevault_enabled` parses `fdesetup status`
("FileVault is On."/"Off.") → `bool`, anything else → `None`. Add a
module docstring stating this is unverified — no Mac hardware (#43) — and that a
consumer must refuse `--install` when `filevault_enabled()` is True (headless
boot is impossible with FileVault; handled in the cli task).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/boot_macos.py tests/test_boot_adapters.py
git commit -m "feat(adapters): macOS boot daemon plist + FileVault detection (unverified)"
```

---

### Task 7: `crr reachable-at-boot` — report, install, uninstall

**Files:**
- Modify: `crr/cli.py`
- Test: `tests/test_boot_adapters.py`

**Interfaces:**
- Consumes: `boot_survival.interpret_boot`, `boot_windows`, `boot_linux`,
  `boot_macos`, `host.is_wsl`, `cli._run_commands`, `cli._load_config`,
  `state_dir`
- Produces: `cli._cmd_reachable_at_boot(args) -> int`; a `reachable-at-boot`
  subparser with `--install` / `--uninstall`; `cli._boot_facts(system, wsl, config)`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_boot_adapters.py`:

```python
from crr import cli
from crr.adapters.boot_windows import BootFacts


def _cfg():
    return {"boot_headless_window_seconds": 300, "boot_preferred_tailnet": "",
            "interop_timeout_seconds": 5, "dashboard_port": 8765}


def test_report_says_headless_when_the_facts_show_it(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli.boot_windows, "read_facts",
                        lambda **k: BootFacts(0.0, 39.0, None, True, False))
    assert cli.main(["reachable-at-boot"]) == 0
    out = capsys.readouterr().out.lower()
    assert "headless" in out and "survivable" in out


def test_report_never_claims_headless_on_unknown(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli.boot_windows, "read_facts",
                        lambda **k: BootFacts(None, None, None, None, None))
    cli.main(["reachable-at-boot"])
    out = capsys.readouterr().out.lower()
    assert "headless" not in out
    assert "unknown" in out or "could not" in out


def test_install_refuses_without_a_tty(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["reachable-at-boot", "--install"]) != 0
    assert ran == []


def test_install_runs_the_generated_script_once_confirmed(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli, "_wsl_distro_and_user", lambda: ("Ubuntu-24.04", "evan"))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["reachable-at-boot", "--install"]) == 0
    assert ran, "confirmed but ran nothing"
    # the elevated register goes through powershell RunAs (mirrors harden)
    assert any("RunAs" in " ".join(c) for c in ran)


def test_macos_install_refuses_under_filevault(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli.boot_macos, "filevault_enabled", lambda **k: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = cli.main(["reachable-at-boot", "--install"])
    assert rc != 0
    assert "filevault" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -k "report or install or filevault_refuse" -v`
Expected: FAIL — `argument command: invalid choice: 'reachable-at-boot'`

- [ ] **Step 3: Write minimal implementation**

Add the subparser and `_cmd_reachable_at_boot` to `crr/cli.py`, importing
`boot_windows`, `boot_linux`, `boot_macos` into the adapter-import block and
`boot_survival` into the core-import block.

- `_boot_facts(system, wsl, config)`: `wsl` → `boot_windows.read_facts(...)`;
  `system == "Linux"` → `boot_linux.read_facts(user, ...)`; `Darwin` → all-None
  facts (unverified); else raise `NotImplementedError`.
- Report path (no flag): build facts, call `interpret_boot(...)` with the config
  window, print the verdict's detail. On WSL, also print the locked/autologin
  facts. Return 0.
- `--install`: refuse without a tty (`return 3`). On WSL: detect distro/user via
  a new `_wsl_distro_and_user()` helper (`wsl.exe -l -v` parse / `whoami`),
  resolve the preferred tailnet (config or the currently-active account), write
  the `install_script(...)` to a temp file, and run it elevated by mirroring
  `harden_windows._elevated_reg_add`'s wrapper: an unelevated `powershell.exe`
  that `Start-Process powershell -File <script> -Verb RunAs -Wait -PassThru;
  if (-not $p) { exit 1 }; exit $p.ExitCode`. Confirm with `input()` then
  `_run_commands`. On Linux: ensure linger via the existing systemd linger
  command; report. On macOS: if `boot_macos.filevault_enabled()` is True, print
  a FileVault refusal to stderr and `return 2`; else install the LaunchDaemon
  (unverified path).
- `--uninstall`: WSL → elevated `Unregister-ScheduledTask` for both task names;
  Linux → leave linger (other services may rely on it), say so; macOS → remove
  the daemon.

Follow `_cmd_harden` / `_cmd_harden_apply` verbatim for the tty-gate + confirm +
`_run_commands` shape.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Verify on this real host — READ ONLY, DO NOT --install**

Run: `.venv/bin/crr reachable-at-boot`
Expected: reports **headless** (the tasks are already installed by hand; WSL
came up ~39s after boot), names the locked-desktop / no-autologin facts, and
does not claim anything it did not read. Do NOT run `--install`. Report the
output verbatim.

- [ ] **Step 6: Commit**

```bash
git add crr/cli.py tests/test_boot_adapters.py
git commit -m "feat(cli): crr reachable-at-boot — report, install (tty+UAC), uninstall"
```

---

### Task 8: `crr doctor` carries the boot verdict

**Files:**
- Modify: `crr/cli.py`
- Test: `tests/test_boot_adapters.py`

**Interfaces:**
- Consumes: `_boot_facts`, `interpret_boot`, `cli._check`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_boot_adapters.py`:

```python
def test_doctor_reports_the_boot_verdict(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli.boot_windows, "read_facts",
                        lambda **k: BootFacts(0.0, 39.0, None, True, False))
    monkeypatch.setattr(cli.state_dir, "state_dir",
                        lambda: __import__("pathlib").Path("/tmp"))
    cli.main(["doctor"])
    out = capsys.readouterr().out.lower()
    assert "reachable at boot" in out and "headless" in out


def test_doctor_shows_login_only_as_a_warning_not_ok(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli.boot_windows, "read_facts",
                        lambda **k: BootFacts(0.0, 28800.0, 28795.0, True, False))
    monkeypatch.setattr(cli.state_dir, "state_dir",
                        lambda: __import__("pathlib").Path("/tmp"))
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "WARN" in out and "reachable at boot" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_boot_adapters.py -k doctor_reports_the_boot -v`
Expected: FAIL — no "reachable at boot" line in doctor

- [ ] **Step 3: Write minimal implementation**

In `_cmd_doctor`, after the Windows Update block, add a "reachable at boot"
check: build facts via `_boot_facts`, `interpret_boot`, and render with
`_check("reachable at boot", ok, detail)` where `ok` is `True` for
`"headless"`, `False` for `"login_only"`, and `None` for `"unknown"` — the same
tri-state `_check` rendering harden uses. Guard the whole block so a platform
with no adapter (`NotImplementedError`) is silently skipped, not a crash.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass

- [ ] **Step 5: Verify on this real host**

Run: `.venv/bin/crr doctor | grep -i -A1 "reachable at boot"`
Expected: `[ok] reachable at boot — ... came up ~39s after boot, before any
login`. Report verbatim.

- [ ] **Step 6: Commit**

```bash
git add crr/cli.py tests/test_boot_adapters.py
git commit -m "feat(cli): doctor reports the reachable-at-boot verdict"
```

---

## Self-Review

**Spec coverage.** Report/install/measure command → Tasks 7, 8. WSL two tasks →
Task 3, registration Task 7. Linux linger + already-works → Task 5, ensured in
Task 7 install. macOS LaunchDaemon + FileVault refusal → Tasks 6, 7. The
headless/login-only/unknown verdict → Task 2, rendered Tasks 7–8. Named config
priors → Task 1. Security facts (locked, autologin) in output → Tasks 4, 7.
Selection is_wsl-first → Task 7. "Never claim more than verified" → Tasks 2, 7,
8 (unknown never renders headless).

**No real registration/reboot in any test** → every install test injects
`_run_commands`; generation is text-only (Task 3). Read paths return all-`None`
on failure and are verified read-only against the host (Tasks 4, 7, 8).

**Placeholder scan.** Tasks 1–3 and 6–8 carry complete code. Tasks 4 and 5 give
the exact dataclass, signatures, the parse/compose rules, and the specific
system sources (`/proc/1`, `loginctl show-user`, `systemctl --user show
ActiveEnterTimestamp`, `last -F`) rather than pasting a full interop builder
whose quoting must match the host — the tests pin the observable behaviour
(all-None on failure, epoch parsing) and Step 5 verifies the real read.

**Type consistency.** `BootFacts(machine_boot, surface_boot, first_login,
locked, autologin)` defined in Task 4 and reused by Tasks 5, 7, 8.
`BootVerdict(status, detail)` and `interpret_boot(machine_boot, surface_boot,
first_login, window_seconds)` from Task 2 are consumed unchanged in 7–8.
`install_script(distro, linux_user, tailnet, script_path)` / `WSL_BOOT_TASK` /
`TAILNET_TASK` from Task 3 are used in Task 7. Config keys
`boot_headless_window_seconds` / `boot_preferred_tailnet` from Task 1 are read in
Task 7's `_cfg`.
