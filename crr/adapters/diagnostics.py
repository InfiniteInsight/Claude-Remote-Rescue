"""Diagnostics source adapter (Linux journald).

Thin, timeout-guarded journalctl queries feeding the pure payload
assembly in ``crr.core.diagnostics``. Each source can fail independently;
the composition root catches per-source and records the failure in the
payload's ``degraded`` list rather than aborting the whole diagnosis.
(macOS ``log show``/``pmset`` and Windows Event Log adapters arrive with
those platforms.)
"""

from __future__ import annotations

import shutil
import subprocess

from crr.core import config as cfg
from crr.core import diagnostics as core

SOURCE_NAME = "journald"

# Kernel/host death signatures — OOM (the WSL-VM scenario) and clean/forced
# shutdown + watchdog events.
_HOST_EVENT_PATTERN = (
    "oom-killer|Out of memory|Killed process|"
    "[Ss]hutting down|reboot|power-off|watchdog did not stop"
)

_ERRS = (subprocess.SubprocessError, OSError, RuntimeError, ValueError)


def available() -> bool:
    return shutil.which("journalctl") is not None


def collect(config: cfg.Config) -> tuple[list, list, list, list]:
    """Query every journald source, degrading (never raising) per source.

    Returns ``(boots, prev_boot_errors, host_events, degraded)``. A source
    that raises is recorded in ``degraded`` rather than aborting the others
    or silently emitting empties.
    """
    timeout = config.get("interop_timeout_seconds")
    lookback = config.get("diagnose_lookback_boots")
    event_cap = config.get("diagnose_event_cap")
    line_cap = config.get("diagnose_line_cap")

    if not available():
        return [], [], [], ["boots", "prev_boot_errors", "host_events"]

    boots: list = []
    prev: list = []
    events: list = []
    degraded: list = []
    try:
        boots = list_boots(event_cap, timeout)
    except _ERRS:
        degraded.append("boots")
    try:
        prev = prev_boot_errors(lookback, line_cap, timeout)
    except _ERRS:
        degraded.append("prev_boot_errors")
    try:
        events = host_events(lookback, event_cap, timeout)
    except _ERRS:
        degraded.append("host_events")
    return boots, prev, events, degraded


def _run(args: list[str], timeout: float) -> str:
    result = subprocess.run(
        ["journalctl", "--no-pager", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"journalctl {args} exited {result.returncode}")
    return result.stdout


def list_boots(cap: int, timeout: float) -> list[dict]:
    return core.parse_boots(_run(["--list-boots", "-o", "json"], timeout), cap)


def prev_boot_errors(lookback: int, line_cap: int, timeout: float) -> list[str]:
    out = _run(["-b", f"-{lookback}", "-p", "err", "-o", "cat", "-n", str(line_cap)], timeout)
    return [line for line in out.splitlines() if line.strip()]


def host_events(lookback: int, cap: int, timeout: float) -> list[str]:
    out = _run(
        ["-b", f"-{lookback}", "-o", "cat", "-g", _HOST_EVENT_PATTERN, "-n", str(cap)],
        timeout,
    )
    return [line for line in out.splitlines() if line.strip()]
