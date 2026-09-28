"""Windows/WSL side of reachable-at-boot: generate the boot Scheduled Tasks,
and read the boot/login facts that prove whether they fired.

Validated by hand on the reference host: an AtStartup S4U task running
`wsl.exe ... exec sleep infinity` brought WSL + the dashboard up 39s after a
cold boot with no login and the desktop still locked. This generates that
reproducibly. NOTHING here registers a task; the cli runs the generated script
elevated after a confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crr.adapters._proc import run_capture as _run
from crr.core.holder_hold import BOOT_MATCH_TOLERANCE_SECONDS

WSL_BOOT_TASK = "crr-wsl-boot"
TAILNET_TASK = "crr-tailnet-default"
# The hold flag sits next to the launcher script (#138 follow-up); the
# launcher finds it via $PSScriptRoot, crr via the launcher's directory.
HOLD_FILE_NAME = "holder-hold.json"

_WSL = r"C:\Windows\System32\wsl.exe"


def wsl_boot_argument(distro: str, linux_user: str) -> str:
    """Args to wsl.exe that boot the distro and hold the VM open forever."""
    return f'-d {distro} -u {linux_user} -e sh -c "exec sleep infinity"'


def holder_script(distro: str, linux_user: str) -> str:
    """PowerShell launcher the holder task runs instead of wsl.exe directly.

    The task re-arms every minute, so a deliberate shutdown needs a way to
    keep it down — decided HERE, on the Windows side, because touching
    anything inside WSL would boot it. Mirrors ``crr.core.holder_hold``:
    only a v1 flag holds, and any failure reading it lets the holder run
    (fail open — a stranded, unreachable machine is the worse failure),
    except that an explicit stop-until-reboot whose boot time can't be read
    keeps holding, as the core does.
    """
    return (
        "# crr: WSL holder launcher (generated; #138). Starts the distro and\n"
        "# holds it open unless holder-hold.json says to stay down.\n"
        f"$hold = Join-Path $PSScriptRoot '{HOLD_FILE_NAME}'\n"
        "try {\n"
        "    if (Test-Path -LiteralPath $hold) {\n"
        "        $h = Get-Content -Raw -LiteralPath $hold | ConvertFrom-Json\n"
        "        if ($h.v -eq 1) {\n"
        "            $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()\n"
        "            if ($h.mode -eq 'stop') { exit 0 }\n"
        "            if ($h.mode -eq 'pause' -and [int64]$h.until -gt $now) { exit 0 }\n"
        "            if ($h.mode -eq 'until-reboot') {\n"
        "                $boot = $null\n"
        "                try { $boot = [DateTimeOffset]::new((Get-CimInstance "
        "Win32_OperatingSystem).LastBootUpTime).ToUnixTimeSeconds() } catch { }\n"
        "                if ($null -eq $boot -or [math]::Abs([int64]$h.boot - $boot) "
        f"-le {BOOT_MATCH_TOLERANCE_SECONDS}) {{ exit 0 }}\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "} catch { }\n"
        f"& '{_WSL}' {wsl_boot_argument(distro, linux_user)}\n"
    )


def end_holder_command() -> list[str]:
    """Stop the running holder (unelevated works; measured on the reference
    host — the Linux-side ``sleep infinity`` dies with it)."""
    return ["schtasks.exe", "/end", "/tn", WSL_BOOT_TASK]


def run_holder_command() -> list[str]:
    """Start the holder now instead of waiting for the next re-arm."""
    return ["schtasks.exe", "/run", "/tn", WSL_BOOT_TASK]


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


def _register_block(task: str, execute: str, argument: str,
                    rearm_minutes: int | None = None) -> str:
    # One AtStartup / S4U / Highest task, unbounded run time. -Force makes
    # re-install idempotent.
    #
    # rearm_minutes repeats the startup trigger forever (a -Once trigger's
    # Repetition with no duration == indefinitely; measured on the reference
    # host). With IgnoreNew a repeat is a no-op while the task still runs,
    # and restarts it once it has died -- e.g. a `sudo reboot` inside WSL
    # restarts the distro, not Windows, and kills the holder (#138).
    repeat = ""
    instances = ""
    if rearm_minutes is not None:
        repeat = (
            "$t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) "
            f"-RepetitionInterval (New-TimeSpan -Minutes {int(rearm_minutes)})).Repetition\n"
        )
        instances = "-MultipleInstances IgnoreNew "
    return (
        f"$a = New-ScheduledTaskAction -Execute '{execute}' -Argument '{argument}'\n"
        "$t = New-ScheduledTaskTrigger -AtStartup\n"
        f"{repeat}"
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries {instances}-ExecutionTimeLimit ([TimeSpan]::Zero)\n"
        "$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U "
        "-RunLevel Highest\n"
        f"Register-ScheduledTask -TaskName '{task}' -Action $a -Trigger $t "
        "-Settings $s -Principal $p -Force | Out-Null\n"
    )


def _write_script_block(path: str, body: str) -> str:
    """PowerShell that writes ``body`` to ``path``, creating its directory."""
    safe = body.replace("'", "''")
    return (
        f"$dir = Split-Path -Parent '{path}'\n"
        "New-Item -ItemType Directory -Force -Path $dir | Out-Null\n"
        f"Set-Content -Path '{path}' -Value '{safe}' -Encoding ASCII\n"
    )


def install_script(distro: str, linux_user: str, tailnet: str | None,
                   script_path: str, *, holder_rearm_minutes: int,
                   holder_script_path: str) -> str:
    """The full PowerShell the cli runs elevated to register the task(s).

    ``holder_rearm_minutes`` (config ``boot_holder_rearm_minutes``) is how
    soon the WSL holder comes back after the distro dies without Windows
    rebooting. Only the holder repeats; the tailnet switch stays one-shot.
    """
    parts = ["$ErrorActionPreference = 'Stop'\n"]
    parts.append(_write_script_block(holder_script_path,
                                     holder_script(distro, linux_user)))
    parts.append(_register_block(
        WSL_BOOT_TASK, "powershell.exe",
        f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{holder_script_path}\"',
        rearm_minutes=holder_rearm_minutes))
    if tailnet:
        # Write the retry script next to where the task will call it, then
        # register the task that runs it.
        parts.append(_write_script_block(script_path, tailnet_script(tailnet)))
        parts.append(_register_block(
            TAILNET_TASK, "powershell.exe",
            f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{script_path}\"'))
    return "".join(parts)


# ---------------------------------------------------------------------------
# READ half: the facts that prove whether the boot tasks above actually
# fired. Every read failure yields None here, never a guess -- an unreadable
# timestamp must render as `unknown` in the core verdict, not a false
# "headless" (spec 2026-08-14, Task 4).
# ---------------------------------------------------------------------------

# No config injection point on this interface (`read_facts(run=None)`);
# fixed rather than tunable, matching the other unelevated interop reads in
# this codebase (e.g. harden_windows' single-call timeout is caller-supplied
# because that read_state() *does* take one -- this one's signature doesn't).
_INTEROP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class BootFacts:
    machine_boot: float | None
    surface_boot: float | None
    # earliest session/explorer.exe start -- NOT a proven interactive
    # unlock. On an ARSO (Automatic Restart Sign-On) host, Windows
    # establishes a LOCKED session at boot, so explorer.exe starts at
    # ~boot with no human present. This field is therefore INFORMATIONAL
    # only; it is deliberately NOT a gate on the "headless" verdict (see
    # crr.core.boot_survival.interpret_boot's fix-round-1 docstring). A
    # proven unlock (Security 4624 type-2 / 4801) would need elevation,
    # above crr's read-only ceiling.
    first_login: float | None
    locked: bool | None
    autologin: bool | None


def parse_epoch(text: str) -> float | None:
    """Parse a single epoch-seconds line; unreadable/empty -> None."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    try:
        return float(line)
    except ValueError:
        return None


def facts_command() -> list[str]:
    """Unelevated, READ-ONLY PowerShell that prints four lines:

    1. Windows boot epoch (``-`` if unreadable)
    2. earliest session/explorer start epoch, taken from the oldest running
       ``explorer.exe`` process -- NOT a proven interactive unlock. On an
       ARSO (Automatic Restart Sign-On) host, Windows brings up a LOCKED
       session at boot and its ``explorer.exe`` starts at ~boot with no
       human present, so this is informational context only, never a gate
       on the headless verdict (a real unlock -- Security 4624 type-2 /
       4801 -- needs elevation, above crr's read-only ceiling). Read from
       ``explorer.exe`` rather than the Security event log because that
       log needs no elevation (``-`` if none)
    3. ``1``/``0`` -- whether ``LogonUI`` (the lock-screen process) is
       running, i.e. whether the desktop is locked
    4. ``1``/``0`` -- whether ``AutoAdminLogon`` is set in the Winlogon key

    Only ``Get-*`` cmdlets -- nothing here can change machine state.

    Epoch conversion deliberately does NOT use ``Get-Date -UFormat %s``.
    Measured read-only on this host (2026-08-14, Eastern Daylight Time,
    UTC-4): ``Get-Date -Date $dt -UFormat %s`` is off by exactly the local
    UTC offset (14400s here) -- it stringifies the local wall-clock digits
    as if they were already UTC instead of converting. Cross-checked
    against ``[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()`` (correct) and
    confirmed by the WSL-boot delta collapsing from a bogus ~4h to the
    expected ~39s once fixed. ``[DateTimeOffset]::new($dt)`` on a
    ``Kind=Local`` ``DateTime`` (what ``Get-CimInstance`` returns here)
    applies the real offset, so ``.ToUnixTimeSeconds()`` is used instead --
    the same "don't trust a culture/format-dependent shortcut" lesson
    ``diagnostics_windows.winevent_command`` already paid for, just hitting
    the Unix-time formatter instead of the date-string formatter this time.
    """
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "if ($os -and $os.LastBootUpTime) "
        "{ [DateTimeOffset]::new($os.LastBootUpTime).ToUnixTimeSeconds() } "
        "else { '-' }; "
        "$ex = Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\"; "
        "if ($ex) { "
        "$first = ($ex | Sort-Object CreationDate | Select-Object -First 1).CreationDate; "
        "[DateTimeOffset]::new($first).ToUnixTimeSeconds() } else { '-' }; "
        "if (Get-Process -Name LogonUI -ErrorAction SilentlyContinue) "
        "{ '1' } else { '0' }; "
        "$wl = Get-ItemProperty "
        "-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon' "
        "-Name AutoAdminLogon -ErrorAction SilentlyContinue; "
        "if ($wl -and $wl.AutoAdminLogon -eq '1') { '1' } else { '0' }"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def _parse_flag(line: str) -> bool | None:
    line = line.strip()
    if line == "1":
        return True
    if line == "0":
        return False
    return None


def _parse_facts_text(
    text: str,
) -> tuple[float | None, float | None, bool | None, bool | None]:
    """Parse ``facts_command``'s four lines. A missing/malformed line is
    ``None`` -- never a guess -- same tri-state discipline as
    ``harden_windows.parse_state``."""
    lines = [line for line in text.splitlines() if line.strip()]
    machine_boot = parse_epoch(lines[0]) if len(lines) > 0 else None
    first_login = parse_epoch(lines[1]) if len(lines) > 1 else None
    locked = _parse_flag(lines[2]) if len(lines) > 2 else None
    autologin = _parse_flag(lines[3]) if len(lines) > 3 else None
    return machine_boot, first_login, locked, autologin


def _wsl_boot_epoch(boot_stat: Path = Path("/proc/1")) -> float | None:
    """systemd (PID 1)'s start time == when this WSL instance booted.

    Read from inside WSL -- no interop round-trip needed for this one field.
    """
    try:
        return boot_stat.stat().st_ctime
    except OSError:
        return None


def read_facts(run=None) -> BootFacts:
    """Gather the boot/login facts the reachable-at-boot verdict needs.

    ``run`` is injectable (signature ``(argv, timeout) -> str``) so tests
    never spawn a real ``powershell.exe``. Any exception from ``run`` --
    missing binary, timeout, nonzero exit -- yields an all-``None``
    ``BootFacts`` (the spine rule: every read failure is an honest
    ``unknown``, never a guess that could render a false "headless").
    ``surface_boot`` is only read after the interop call succeeds, so a
    probe failure stays all-``None`` end to end.
    """
    run = run or _run
    try:
        text = run(facts_command(), _INTEROP_TIMEOUT_SECONDS)
    except Exception:
        return BootFacts(None, None, None, None, None)
    machine_boot, first_login, locked, autologin = _parse_facts_text(text)
    surface_boot = _wsl_boot_epoch()
    return BootFacts(machine_boot, surface_boot, first_login, locked, autologin)
