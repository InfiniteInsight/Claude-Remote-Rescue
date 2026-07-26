"""Plain-English death summary (pure core).

Turns the raw host-death / error lines the diagnostics adapters collect
(journald, WinEvent, WSL OOM) into a few human sentences — the "translated
to plain English" the README promises. Each sentence is an inference from a
log signature and is framed as such ("appears", "was"), severity-ordered and
deduped. When nothing matches, the verdict is an explicit "looks clean",
never silence (which would read as "no data").

Pure: it takes already-collected lines, so it is fully testable and adds no
new subprocess. Consumed by ``build_payload`` (the ``summary`` field of the
versioned /api/diagnostics contract) so both the CLI and the dashboard render
the same words.
"""

from __future__ import annotations

import re
from typing import Sequence

# OOM detection terms — the SINGLE source shared with the diagnostics
# collectors (adapters/diagnostics_windows), so what lands in host_events and
# what the verdict recognizes can never drift apart.
OOM_TERMS = ("out of memory", "oom-killer", "killed process")

# (pattern, sentence) pairs, declared MOST-SEVERE FIRST — summarize emits any
# that match in this order, so declaration order IS the reporting order.
_SIGNATURES: list[tuple[re.Pattern[str], str]] = [
    (re.compile("|".join(OOM_TERMS), re.I),
     "Out-of-memory: the host ran low on memory and the kernel killed one or "
     "more processes. On WSL, check the Shmem / Inactive(anon) figures — the "
     "victim is often shared/tmpfs memory, not the biggest process."),
    (re.compile(r"panic", re.I),
     "Kernel panic: the host crashed."),
    (re.compile(r"unexpected|6008|kernel-power|\b41\b|power[- ]?loss", re.I),
     "Unexpected shutdown: the host lost power or crashed — no clean shutdown "
     "was recorded before it went down."),
    (re.compile(r"watchdog", re.I),
     "Watchdog reset: a watchdog timer restarted the host."),
    # Actual shutdown phrases only — NOT a bare "reboot"/"restart", which also
    # matches noise like `@reboot` cron jobs or `ua-reboot-cmds.service`.
    (re.compile(
        r"shutting down|systemd-shutdown|rebooting|power(?:ed)?[- ]?off|poweroff|"
        r"halt(?:ing|ed)?\b|1074|reached target (?:shutdown|reboot|power)", re.I),
     "Clean shutdown/restart: the host was shut down or rebooted normally "
     "(a planned reboot or an update, not a crash)."),
]

_CLEAN = (
    "No crash, out-of-memory, or shutdown signature was recognized in the "
    "collected events — the previous boot looks clean. A session that died "
    "here most likely lost its terminal, not the host."
)


def summarize(host_events: Sequence[str], prev_boot_errors: Sequence[str]) -> list[str]:
    """Return the matching plain-English death summaries, most severe first.

    Scans both the host-death events and the previous-boot error lines (an OOM
    can surface in either). Always returns at least one sentence: the explicit
    "looks clean" verdict when nothing matches. Order and uniqueness come for
    free from ``_SIGNATURES`` (declared severity-desc, distinct sentences).
    """
    haystack = "\n".join([*host_events, *prev_boot_errors])
    return [sentence for pattern, sentence in _SIGNATURES if pattern.search(haystack)] or [_CLEAN]
