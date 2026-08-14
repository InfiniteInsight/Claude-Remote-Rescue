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
