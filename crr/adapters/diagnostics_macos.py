"""Diagnostics source adapter (macOS: unified log + pmset).

The macOS counterpart to the journald adapter. DESIGN scopes macOS to the
death signal: ``log show`` filtered to shutdown/panic/watchdog and ``pmset
-g log`` for sleep/wake/thermal — both feeding ``host_events``. macOS has
no clean journald-style analogue for the other two fields, so:

- ``boots`` is just the current boot (``sysctl -n kern.boottime``) — one
  honest record, not a fabricated history.
- ``prev_boot_errors`` has no cheap bounded source on macOS and is always
  degraded, rather than mounting an expensive full-log error scan.

``host_events`` is strict on purpose: if EITHER ``log show`` or ``pmset``
fails, the whole field degrades — so a green ``host_events`` genuinely
means both sub-sources ran (the swallowed-exit-code lesson, guarded).

Same signatures/attrs the composition root expects of a diagnostics
source: ``SOURCE_NAME``, ``available()``, ``collect(config)``.
"""

from __future__ import annotations

import subprocess

from crr.core import config as cfg
from crr.core import diagnostics as core

SOURCE_NAME = "log+pmset"

# `log show` predicate bounds the query server-side (an unfiltered 1d window
# is huge); the client-side filter below is redundant insurance.
_LOG_PREDICATE = (
    'eventMessage CONTAINS[c] "panic" '
    'OR eventMessage CONTAINS[c] "shutdown" '
    'OR eventMessage CONTAINS[c] "watchdog"'
)
_LOG_TERMS = ("panic", "shutdown", "watchdog")
_PMSET_TERMS = ("Sleep", "Wake", "DarkWake", "Thermal")

_ERRS = (subprocess.SubprocessError, OSError, RuntimeError, ValueError)


def available() -> bool:
    # `log`, `pmset`, `sysctl` ship with macOS; `log` is the load-bearing one.
    import shutil
    return shutil.which("log") is not None


def _run(argv: list[str], timeout: float) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{argv[0]} exited {result.returncode}")
    return result.stdout


def _current_boot(timeout: float) -> list:
    return core.parse_mac_boottime(_run(["sysctl", "-n", "kern.boottime"], timeout))


def _host_events(window: str, cap: int, timeout: float) -> list:
    log_out = _run(
        ["log", "show", "--last", window, "--style", "compact", "--predicate", _LOG_PREDICATE],
        timeout,
    )
    pmset_out = _run(["pmset", "-g", "log"], timeout)
    events = core.filter_lines(log_out, _LOG_TERMS, cap)
    events += core.filter_lines(pmset_out, _PMSET_TERMS, cap)
    return events[:cap] if cap else events


def collect(config: cfg.Config) -> tuple[list, list, list, list]:
    """Return ``(boots, prev_boot_errors, host_events, degraded)`` for macOS."""
    timeout = config.get("diagnose_macos_timeout_seconds")
    window = config.get("diagnose_macos_lookback")
    event_cap = config.get("diagnose_event_cap")

    # prev_boot_errors has no cheap macOS source — always degraded, honestly.
    degraded: list = ["prev_boot_errors"]
    if not available():
        return [], [], [], ["boots", "prev_boot_errors", "host_events"]

    boots: list = []
    events: list = []
    try:
        boots = _current_boot(timeout)
    except _ERRS:
        degraded.append("boots")
    try:
        events = _host_events(window, event_cap, timeout)
    except _ERRS:
        degraded.append("host_events")
    return boots, [], events, degraded
