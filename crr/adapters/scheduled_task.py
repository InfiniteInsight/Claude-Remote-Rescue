"""Windows Scheduled Task watcher (WSL host) — the Windows analogue of the
systemd/launchd adapters.

crr runs in WSL, so the watchdog is a Windows Scheduled Task that re-enters
the distro (``wsl.exe -e``) to run ``crr revive`` on an interval and ``crr
web`` at logon. Ported from ccresume's VBS-less hidden Scheduled Task (the
task runs windowless in the background — no console pops up).

Pure ``schtasks.exe`` command builders (data, not run): the composition root
prints them by default and only executes on explicit ``--install``.
schtasks' minimum interval is 1 minute, so a sub-minute watchdog interval is
rounded up (and that rounding is surfaced, not hidden).

UNVERIFIED from Linux CI: builders are unit-tested; the real schtasks
round-trip is author-verified on Windows (task #8).

One read-only probe does run here: ``crr_tasks_installed`` asks
``schtasks.exe /Query`` whether crr's tasks exist, for ``crr service`` (#161).
"""

from __future__ import annotations

import shutil
import subprocess
import time

REVIVE_TASK = "CRR Revive"
WEB_TASK = "CRR Web"


def _wsl_invocation(crr_bin: str, args: list[str], distro: str | None) -> str:
    """The /TR command string: re-enter WSL and run ``crr <args>``."""
    parts = ["wsl.exe"]
    if distro:
        parts += ["--distribution", distro]
    parts += ["-e", crr_bin, *args]
    return " ".join(parts)


def interval_minutes(interval_seconds: int) -> int:
    """schtasks granularity is whole minutes; round up, min 1."""
    return max(1, (interval_seconds + 59) // 60)


def create_revive_task_command(
    crr_bin: str, interval_seconds: int, distro: str | None = None
) -> list[str]:
    # NOTE (verify on the Windows/WSL hardware test): systemd needed
    # KillMode=process so its cgroup cleanup didn't reap the tmux the revive
    # spawns. Here `wsl.exe -e crr revive` runs the revive inside the distro
    # and exits; the detached tmux server should keep the distro (and itself)
    # alive. Confirm a revived session survives the task completing; if not,
    # the revive may need to nohup/setsid-detach the tmux from the wsl.exe
    # invocation's lifetime.
    tr = _wsl_invocation(crr_bin, ["revive"], distro)
    return [
        "schtasks.exe", "/Create", "/TN", REVIVE_TASK, "/TR", tr,
        "/SC", "MINUTE", "/MO", str(interval_minutes(interval_seconds)), "/F",
    ]


def create_web_task_command(
    crr_bin: str, port: int, distro: str | None = None
) -> list[str]:
    tr = _wsl_invocation(crr_bin, ["web", "--port", str(port)], distro)
    return ["schtasks.exe", "/Create", "/TN", WEB_TASK, "/TR", tr, "/SC", "ONLOGON", "/F"]


def query_task_command(name: str) -> list[str]:
    """Read-only: exits 0 when the task ``name`` exists."""
    return ["schtasks.exe", "/Query", "/TN", name]


def crr_tasks_installed(timeout: float) -> bool:
    """Is crr installed as Windows Scheduled Tasks (either of its two tasks)?

    Both queries share one ``timeout`` budget. No schtasks.exe, a failure or
    a timeout all mean "not found": this only decides how ``crr service``
    describes a host with no systemd units, and must never raise.
    """
    if shutil.which("schtasks.exe") is None:
        return False
    deadline = time.monotonic() + timeout
    for name in (REVIVE_TASK, WEB_TASK):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            r = subprocess.run(query_task_command(name), capture_output=True,
                               text=True, timeout=remaining)
        except (subprocess.SubprocessError, OSError):
            return False
        if r.returncode == 0:
            return True
    return False


def delete_task_commands() -> list[list[str]]:
    return [
        ["schtasks.exe", "/Delete", "/TN", REVIVE_TASK, "/F"],
        ["schtasks.exe", "/Delete", "/TN", WEB_TASK, "/F"],
    ]
