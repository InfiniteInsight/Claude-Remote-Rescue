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
