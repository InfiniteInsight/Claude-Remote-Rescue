"""Remote-Control bridge-drop predicate (spec — dropped-Remote-Control
watchdog, Part B).

Pure core: decides whether a session's mobile Remote Control link looks
dropped, given two already-sampled facts (how many transcript records sit
after the newest ``bridge-session`` marker, and whether one was ever seen)
and a configured staleness threshold. No I/O, no clock — mirrors
``crr.core.takeover.ready_to_take_over``'s shape: the adapter samples, this
module only judges.

Why the count is in RECORDS and not seconds: an idle session writes
NOTHING while the user is just away from the keyboard, so a time-based
rule would fire on every session anyone ever walked away from — the
overwhelming common case, not a drop. Counting records is self-
normalising: the counter only advances when claude is actually producing
work, which is exactly the "dropped mid-work" case worth acting on, and it
falls silent (never climbs) on a merely idle one.

Honest calibration note: ``bridge-session`` markers are written as part of
the per-user-prompt metadata block (``user, attachment, last-prompt,
ai-title, mode, permission-mode, pr-link, bridge-session``), NOT on some
independent bridge-health cadence. So this really measures "records since
the last user prompt", not "records since the bridge was last seen" — a
subtle but real difference. One very long agentic turn (a subagent
fan-out, a long tool-call loop) followed by idle can accumulate many
records with no NEW prompt (and so no new marker) while the bridge itself
never dropped, and would read as ``dropped`` all the same. The threshold
(``bridge_stale_records``, see ``crr.core.config``) is measured against
real gaps and still holds with margin — this note documents what the
number actually counts, not a change to it.
"""

from __future__ import annotations


def bridge_state(records_since_marker: int, had_marker: bool, *, stale_after: int) -> str:
    """Classify the Remote Control bridge as ``"off"``, ``"ok"``, or
    ``"dropped"``.

    - ``"off"``: ``had_marker`` is False — no ``bridge-session`` record was
      ever seen, so Remote Control was never enabled on this session. You
      cannot drop what was never up: this holds regardless of
      ``records_since_marker`` (the adapter reports ``0`` when no marker
      was found within its scan window, but any value would be ignored
      here just the same).
    - ``"dropped"``: a marker was seen, and more than ``stale_after``
      records have been written since — the bridge came up and then went
      quiet while claude kept working.
    - ``"ok"``: a marker was seen and the transcript is within the
      threshold.
    """
    if not had_marker:
        return "off"
    if records_since_marker > stale_after:
        return "dropped"
    return "ok"
