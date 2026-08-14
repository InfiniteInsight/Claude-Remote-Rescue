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

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

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


# ``diagnostics_windows.winevent_command`` now formats TimeCreated itself
# as invariant ``yyyy-MM-dd HH:mm:ss`` (fix round 1, Important 3), so ISO is
# the authoritative, locale-safe shape for anything that actually came from
# that command. The other formats below are kept only as a FALLBACK for
# lines that didn't -- e.g. a real host measured 2026-08-13, before that
# fix, rendering PowerShell's culture-dependent default as
# "08/09/2026 08:44:39" (en-US, 24h). Trusting only ISO there would have
# parsed zero timestamps and printed a false "no restarts outside the
# window" -- succeeding loudly while measuring nothing. This is still not
# exhaustive of every .NET culture (a DD/MM host's "08/09/2026" would still
# misparse as Aug 9 rather than Sep 8 under the en-US fallback -- fixing
# the source removes that ambiguity for lines that use it, not for these
# fallback formats); an unrecognized shape correctly falls through to "no
# parseable timestamp" rather than a guess.
_TIMESTAMP_RE = re.compile(
    r"^(\d{1,4}[-/]\d{1,2}[-/]\d{1,4} \d{1,2}:\d{2}:\d{2}(?:\s*[AaPp][Mm])?)")
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",     # ISO 24h
    "%m/%d/%Y %H:%M:%S",     # en-US, 24h -- observed on a real host
    "%m/%d/%Y %I:%M:%S %p",  # en-US, 12h + AM/PM
)


def _parse_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP_RE.match(line.strip())
    if match is None:
        return None
    raw = re.sub(r"\s+", " ", match.group(1)).strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parsed_count(events) -> int:
    """How many of these lines had a parseable leading timestamp.

    Lets a caller tell "no events" apart from "events, but none of them
    had a timestamp this module recognizes" -- the second is a format
    this module doesn't know yet, not a clean measurement, and must not
    render the same as one.
    """
    return sum(1 for line in events if _parse_timestamp(line) is not None)


def restarts_outside(events, start: int, end: int) -> tuple[str, ...]:
    """Which of these boot/shutdown event lines fall outside the
    ``start``-``end`` active-hours window -- evidence the hardening policy
    did not hold, even after it was applied.

    A line with no parseable leading timestamp cannot support a claim in
    either direction, so it is skipped -- never counted as inside the
    window (which would hide a real failure) or outside it (which would
    invent one).
    """
    hits = []
    for line in events:
        ts = _parse_timestamp(line)
        if ts is None:
            continue
        if not covers(start, end, ts.hour):
            hits.append(line)
    return tuple(hits)


def within_lookback(events, days: int, now: datetime) -> tuple[str, ...]:
    """Event lines timestamped within the last ``days`` days of ``now``.

    Bounds the restart measurement to a recent, meaningful window (an old
    restart from before the policy was ever applied is not evidence about
    whether it holds today). A line with no parseable timestamp cannot
    support a claim about its recency either, so it is skipped -- never
    assumed recent, never assumed stale.
    """
    cutoff = now - timedelta(days=days)
    hits = []
    for line in events:
        ts = _parse_timestamp(line)
        if ts is None:
            continue
        if ts >= cutoff:
            hits.append(line)
    return tuple(hits)
