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
# "oom-kill" covers the kernel's `oom-kill:constraint=...` record, the
# `Failed with result 'oom-kill'` systemd line and `oom-killer` itself;
# "oom killer" the `has been killed by the OOM killer` systemd line (#148).
OOM_TERMS = ("out of memory", "oom-kill", "oom killer", "killed process")
_OOM_RE = re.compile("|".join(OOM_TERMS), re.I)

# The kernel's victim line, e.g.
#   Out of memory: Killed process 21373 (node) total-vm:95301400kB,
#   anon-rss:81837596kB, file-rss:0kB, shmem-rss:0kB, ...
_OOM_VICTIM_RE = re.compile(
    r"Killed process (\d+) \(([^)]*)\)(?:.*?anon-rss:(\d+)kB)?", re.I)


def parse_oom_victims(lines: Sequence[str]) -> list[dict]:
    """Distinct OOM victims in ``lines``, each with how often it was logged.

    WSL2 replays the kernel ring buffer into the journal on every distro
    restart, so ONE kill appears many times under different journal
    timestamps. Victims are keyed by (pid, name, anon-rss) — the kernel's own
    identity for the kill — and reported once with a ``count`` of occurrences,
    in first-seen order. ``rss_kb`` is None when the line carried no figure.
    """
    victims: dict[tuple, dict] = {}
    for line in lines:
        m = _OOM_VICTIM_RE.search(line)
        if not m:
            continue
        rss_kb = int(m.group(3)) if m.group(3) else None
        key = (m.group(1), m.group(2), rss_kb)
        if key in victims:
            victims[key]["count"] += 1
        else:
            victims[key] = {"pid": int(m.group(1)), "name": m.group(2),
                            "rss_kb": rss_kb, "count": 1}
    return list(victims.values())


def _describe_victim(v: dict) -> str:
    text = f"{v['name']} (pid {v['pid']}"
    if v["rss_kb"] is not None:
        text += f", {v['rss_kb'] / (1024 * 1024):.1f} GiB RSS"
    if v["count"] > 1:
        text += f", logged {v['count']} times"
    return text + ")"

# (pattern, sentence) pairs, declared MOST-SEVERE FIRST — summarize emits any
# that match in this order, so declaration order IS the reporting order.
_SIGNATURES: list[tuple[re.Pattern[str], str]] = [
    (_OOM_RE,
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

# The system manager (PID 1) finished starting. Once per distro start: on WSL2
# the VM's kernel boot outlives distro restarts, so several of these inside ONE
# journal boot are the only trace of a restart loop (#148). User managers
# print the same message as `systemd[<pid>]`, hence the literal `[1]`.
_SYSTEM_START_RE = re.compile(r"systemd\[1\]: Startup finished")

_CLEAN = (
    "No crash, out-of-memory, or shutdown signature was recognized in the "
    "collected events — the previous boot looks clean. A session that died "
    "here most likely lost its terminal, not the host."
)


def host_event_rank(line: str) -> int | None:
    """Severity rank of one log line (0 = most severe), or None for noise.

    The index of the first ``_SIGNATURES`` entry the line matches — the same
    patterns ``summarize`` uses, so what is *kept* as an event and what the
    verdict *recognizes* can never drift apart (#148: a bare "reboot" substring
    kept benign `@reboot` cron lines as "events").
    """
    if _SYSTEM_START_RE.search(line):
        # Evidence of a restart, but below every death signature — and never
        # run through them: its timestamp can satisfy e.g. the `\b41\b` event id.
        return len(_SIGNATURES)
    for rank, (pattern, _sentence) in enumerate(_SIGNATURES):
        if pattern.search(line):
            return rank
    return None


def summarize(host_events: Sequence[str], prev_boot_errors: Sequence[str]) -> list[str]:
    """Return the matching plain-English death summaries, most severe first.

    Scans both the host-death events and the previous-boot error lines (an OOM
    can surface in either). Always returns at least one sentence: the explicit
    "looks clean" verdict when nothing matches. Order and uniqueness come for
    free from ``_SIGNATURES`` (declared severity-desc, distinct sentences).
    """
    lines = [*host_events, *prev_boot_errors]
    # Restart markers are counted, not pattern-matched (see host_event_rank).
    haystack = "\n".join(line for line in lines if not _SYSTEM_START_RE.search(line))
    out = []
    for pattern, sentence in _SIGNATURES:
        if not pattern.search(haystack):
            continue
        if pattern is _OOM_RE:
            victims = parse_oom_victims(lines)
            if victims:
                sentence = (
                    "Out-of-memory: the host ran low on memory and the kernel "
                    "killed " + "; ".join(_describe_victim(v) for v in victims) + ".")
        out.append(sentence)
    starts = sum(1 for line in lines if _SYSTEM_START_RE.search(line))
    if starts > 1:
        # More than one start in the one boot being inspected is not "clean":
        # say how often the system manager started so a restart loop is visible.
        out.append(
            f"The system manager (systemd) started {starts} times within this one "
            "kernel boot. On WSL2 each distro restart does this while the VM keeps "
            "running, so repeated restarts do not show up as separate boots.")
    return out or [_CLEAN]
