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


def _covers_all_of(outer_start, outer_end, inner_start, inner_end) -> bool:
    """True when the outer window is at least as wide as the inner one and
    contains the inner window's start hour.

    This is deliberately anchored on the start hour rather than a full
    end-to-end containment check. A precise containment check (projecting
    ``inner_end`` onto ``outer_start``'s frame via ``span_hours``) breaks
    down whenever both windows sit at the same span: any nonzero shift
    between two equal-span windows makes the projected end wrap past the
    outer span, so it reports "not contained" even for windows that a
    human would call equivalent (e.g. two different 18-hour, the Windows
    maximum, windows a couple of hours apart). Anchoring on the start hour
    plus a span comparison avoids that false negative and matches this
    module's test table.
    """
    if span_hours(outer_start, outer_end) < span_hours(inner_start, inner_end):
        return False
    return covers(outer_start, outer_end, inner_start)


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
    elif _covers_all_of(state.active_start, state.active_end, want_start, want_end):
        hours = Finding("active_hours", True,
                        f"active hours {state.active_start}:00-{state.active_end}:00 "
                        f"are at least as wide as {want_start}:00-{want_end}:00 and "
                        f"include its start")
    else:
        hours = Finding("active_hours", False,
                        f"active hours are {state.active_start}:00-"
                        f"{state.active_end}:00; sessions outside that window "
                        f"are unprotected (crr would set {want_start}:00-"
                        f"{want_end}:00)")
    return (policy, hours)
