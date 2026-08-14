"""Windows Update hardening — the pure half (spec 2026-08-12).

crr cannot stop a forced Update restart from inside WSL. What it can do is
apply the two host policies that are supposed to prevent one, and then
MEASURE whether a restart happened anyway. This module is the assessment;
the adapter reads the registry and the cli decides whether to write.

Nothing here says "protected". Microsoft filed these policies under
"Legacy Policies" on Windows 11 and there are credible reports of them
being ignored, so the honest vocabulary is "applied" plus evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

# Windows refuses an active-hours range longer than this.
MAX_ACTIVE_HOURS_SPAN = 18


def span_hours(start: int, end: int) -> int:
    """Length of the active-hours window, honouring a midnight wrap."""
    return (end - start) % 24


def covers(start: int, end: int, hour: int) -> bool:
    """Is ``hour`` inside the window? Start inclusive, end exclusive."""
    if span_hours(start, end) == 0:
        return False
    return span_hours(start, hour) < span_hours(start, end)


def valid_span(start: int, end: int) -> str | None:
    """None when the range is legal, else why Windows would refuse it."""
    for name, value in (("start", start), ("end", end)):
        if not isinstance(value, int) or not 0 <= value <= 23:
            return f"active hours {name} must be an hour from 0 to 23, got {value!r}"
    span = span_hours(start, end)
    if span > MAX_ACTIVE_HOURS_SPAN:
        return (f"active hours {start}:00-{end}:00 spans {span} hours; "
                f"Windows allows at most {MAX_ACTIVE_HOURS_SPAN}")
    return None


@dataclass(frozen=True)
class HardenState:
    """What the host's registry says. ``None`` means it could not be read."""
    policy_set: bool | None
    active_start: int | None
    active_end: int | None
    smart_hours: bool | None


@dataclass(frozen=True)
class Finding:
    key: str
    ok: bool | None        # None = unknown; never coerce it to a bool
    detail: str


def _uncovered_ranges(existing_start, existing_end, want_start, want_end):
    """Which hour ranges inside the wanted window the existing window does
    NOT cover, as a list of (start, end) tuples (start inclusive, end
    exclusive, same convention as ``covers``).

    This asks the only question that matters: "are any wanted hours
    actually unprotected?" -- not "is the wanted window a subset of the
    existing one" (a containment predicate is non-monotonic in the actual
    gap: it can call an 8-hour gap "fine" and a 1-hour gap "not fine"
    depending on where the windows start, which is a green light over a
    real hole). Every hour is checked individually via ``covers``, so the
    verdict tracks the real amount of exposure, not the shape of the
    windows.
    """
    span = span_hours(want_start, want_end)
    hours = [(want_start + i) % 24 for i in range(span)]
    covered = [covers(existing_start, existing_end, h) for h in hours]

    ranges = []
    i = 0
    n = len(hours)
    while i < n:
        if covered[i]:
            i += 1
            continue
        j = i
        while j < n and not covered[j]:
            j += 1
        ranges.append((hours[i], (hours[j - 1] + 1) % 24))
        i = j
    return ranges


def _format_ranges(ranges) -> str:
    return ", ".join(f"{start:02d}:00-{end:02d}:00" for start, end in ranges)


def assess(state: HardenState, want_start: int, want_end: int) -> tuple[Finding, ...]:
    """Findings for each hardening lever, with unknowns kept unknown."""
    if state.policy_set is None:
        policy = Finding("no_auto_reboot", None,
                         "could not read the Windows Update policy key")
    elif state.policy_set:
        policy = Finding("no_auto_reboot", True,
                         "NoAutoRebootWithLoggedOnUsers is set")
    else:
        policy = Finding("no_auto_reboot", False,
                         "NoAutoRebootWithLoggedOnUsers is not set, so Windows "
                         "may restart to finish an update while you are logged on")

    if state.active_start is None or state.active_end is None:
        hours = Finding("active_hours", None, "could not read active hours")
    elif state.smart_hours is None:
        # smart_hours is tri-state too. If we cannot tell whether it is on,
        # we cannot vouch that the configured start/end are what's actually
        # in force -- same spine rule as an unreadable window, applied to
        # the flag that could silently override it.
        hours = Finding("active_hours", None,
                        "could not read whether smart active hours is "
                        "enabled; if it is, it would override the "
                        "configured start/end")
    elif state.smart_hours:
        hours = Finding("active_hours", None,
                        "smart active hours is on, so Windows chooses the "
                        "window itself and the configured values are not "
                        "what is in force")
    else:
        gaps = _uncovered_ranges(state.active_start, state.active_end,
                                 want_start, want_end)
        if not gaps:
            hours = Finding("active_hours", True,
                            f"active hours {state.active_start}:00-"
                            f"{state.active_end}:00 cover all of "
                            f"{want_start}:00-{want_end}:00")
        else:
            hours = Finding("active_hours", False,
                            f"active hours are {state.active_start}:00-"
                            f"{state.active_end}:00; unprotected hours: "
                            f"{_format_ranges(gaps)} "
                            f"(crr would set {want_start}:00-{want_end}:00)")
    return (policy, hours)
