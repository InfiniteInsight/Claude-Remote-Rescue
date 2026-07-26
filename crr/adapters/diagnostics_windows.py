"""Windows/WSL diagnostics source (Phase 4) — why did the host/VM die.

Two signals, both ported from ccresume:

- **Windows host events** via PowerShell ``Get-WinEvent`` for the shutdown
  IDs 1074 (initiated shutdown), 6008 (unexpected shutdown), 41
  (Kernel-Power dirty reboot).
- **WSL-VM OOM forensics.** [lesson: the 90GB that nobody owned] When the WSL
  VM dies to the OOM killer, the culprit is usually NOT the process with the
  biggest RSS — it is shmem/tmpfs and inactive_anon that nobody attributes to
  a process. So the forensics surfaces ``Shmem`` and ``Inactive(anon)`` from
  ``/proc/meminfo`` alongside the dmesg OOM lines, not a per-process RSS view.

The parsers here are pure (synthetic-testable); ``collect`` runs the real
commands. Wiring into the composition root's diagnostics dispatch lands with
the platform-dispatch refactor (PR #11). UNVERIFIED from Linux CI — the real
powershell/dmesg round-trip is author-verified on Windows (task #8).
"""

from __future__ import annotations

import re
import subprocess

SOURCE_NAME = "winevent+wsl-oom"

# Windows System-log event IDs that explain a host death/reboot.
SHUTDOWN_EVENT_IDS = (1074, 6008, 41)

_OOM_RE = re.compile(r"out of memory|oom-killer|killed process", re.IGNORECASE)
_MEMINFO_RE = re.compile(r"^([^:]+):\s+(\d+)\s*kB", re.MULTILINE)
# The bystander fields the RSS view misses, in report order.
_FORENSIC_FIELDS = ("MemTotal", "MemAvailable", "Shmem", "Inactive(anon)")


def winevent_command(ids: tuple[int, ...], cap: int) -> list[str]:
    """PowerShell to pull the shutdown events, one formatted line each."""
    id_list = ",".join(str(i) for i in ids)
    script = (
        f"Get-WinEvent -FilterHashtable @{{LogName='System';Id={id_list}}} "
        f"-MaxEvents {cap} -ErrorAction SilentlyContinue | "
        "ForEach-Object { \"$($_.TimeCreated) [$($_.Id)] $($_.Message)\" }"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def parse_winevents(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_oom_lines(dmesg_text: str) -> list[str]:
    return [line.strip() for line in dmesg_text.splitlines() if _OOM_RE.search(line)]


def parse_meminfo(text: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2)) for m in _MEMINFO_RE.finditer(text)}


def _human_kb(kb: int) -> str:
    mib = kb / 1024
    if mib >= 1024:
        return f"{mib / 1024:.1f}GiB"
    return f"{mib:.0f}MiB"


def format_memory_forensics(meminfo: dict[str, int]) -> str:
    """A one-line breakdown emphasizing the bystander fields, or "".

    Empty when none of the forensic fields are present (nothing to say).
    """
    parts = [f"{f}={_human_kb(meminfo[f])}" for f in _FORENSIC_FIELDS if f in meminfo]
    if not parts:
        return ""
    return ("memory at OOM: " + " ".join(parts)
            + " (shmem/inactive_anon bystanders, not just process RSS)")


_ERRS = (subprocess.SubprocessError, OSError, RuntimeError, ValueError)


def available() -> bool:
    import shutil
    return shutil.which("powershell.exe") is not None or shutil.which("dmesg") is not None


def _run(argv: list[str], timeout: float) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{argv[0]} exited {result.returncode}")
    return result.stdout


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def collect(config) -> tuple[list, list, list, list]:
    """Return ``(boots, prev_boot_errors, host_events, degraded)``.

    boots/prev_boot_errors have no cheap WSL source → degraded. host_events
    combines Windows shutdown events + WSL OOM forensics; a failure of either
    sub-source degrades host_events (so a green host_events means it ran).
    """
    timeout = config.get("interop_timeout_seconds")
    cap = config.get("diagnose_event_cap")
    if not available():
        return [], [], [], ["boots", "prev_boot_errors", "host_events"]
    degraded = ["boots", "prev_boot_errors"]
    events: list = []
    try:
        events += parse_winevents(_run(winevent_command(SHUTDOWN_EVENT_IDS, cap), timeout))
        oom = parse_oom_lines(_run(["dmesg"], timeout))
        events += oom
        if oom:  # only surface the memory breakdown when an OOM actually fired
            forensics = format_memory_forensics(parse_meminfo(_read("/proc/meminfo")))
            if forensics:
                events.append(forensics)
    except _ERRS:
        degraded.append("host_events")
    return [], [], events[:cap] if cap else events, degraded
