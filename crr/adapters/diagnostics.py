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

from crr.adapters._proc import run_capture
from crr.core import config as cfg
from crr.core import diagnostics as core
from crr.core import explain

SOURCE_NAME = "journald"

# Kernel/host death signatures — OOM (the WSL-VM scenario) and clean/forced
# shutdown + watchdog events. journalctl's coarse pre-filter only: every kept
# line is re-classified by ``explain.host_event_rank``. Deliberately NO bare
# "reboot" — it matched `ua-reboot-cmds.service` and `(CRON) ... @reboot`
# lines and filled the event cap with noise (#148).
_HOST_EVENT_PATTERN = (
    "oom-kill|oom killer|Out of memory|Killed process|"
    "[Ss]hutting down|systemd-shutdown|[Rr]ebooting|Reached target "
    "(?:Shutdown|Reboot|Power-?Off)|power-off|watchdog did not stop"
)


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
    except core.DEGRADE_ERRORS:
        degraded.append("boots")
    try:
        prev = prev_boot_errors(lookback, line_cap, timeout)
    except core.DEGRADE_ERRORS:
        degraded.append("prev_boot_errors")
    try:
        events = host_events(lookback, config.get("diagnose_scan_cap"), timeout)
    except core.DEGRADE_ERRORS:
        degraded.append("host_events")
    return boots, prev, events, degraded


def _run(args: list[str], timeout: float) -> str:
    return run_capture(["journalctl", "--no-pager", *args], timeout)


def list_boots(cap: int, timeout: float) -> list[dict]:
    return core.parse_boots(_run(["--list-boots", "-o", "json"], timeout), cap)


def prev_boot_errors(lookback: int, line_cap: int, timeout: float) -> list[str]:
    out = _run(["-b", f"-{lookback}", "-p", "err", "-o", "cat", "-n", str(line_cap)], timeout)
    return [line for line in out.splitlines() if line.strip()]


def host_events(lookback: int, scan_cap: int, timeout: float) -> list[str]:
    """Classified host-death lines from the previous boot, NOT yet event-capped.

    ``scan_cap`` only bounds how much journal is read; the event cap is
    applied after classification and dedupe (``core.select_host_events`` in
    ``build_payload``), so noise or replays cannot crowd out a real signature.
    """
    out = _run(
        ["-b", f"-{lookback}", "-o", "cat", "-g", _HOST_EVENT_PATTERN, "-n", str(scan_cap)],
        timeout,
    )
    return [line for line in out.splitlines()
            if line.strip() and explain.host_event_rank(line) is not None]
