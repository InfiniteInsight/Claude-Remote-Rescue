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


def bridge_state(records_since_marker: int, had_marker: bool | None, *, stale_after: int) -> str:
    """Classify the Remote Control bridge as ``"unknown"``, ``"off"``,
    ``"ok"``, or ``"dropped"``.

    - ``"unknown"``: ``had_marker`` is None — the adapter did not finish
      looking (its scan window ran out before the transcript did, the
      caller opted out with ``bridge_scan_lines=0``, or the transcript was
      unreadable). Absence of evidence, not evidence of absence:
      ``records_since_marker`` is meaningless without a marker to measure
      FROM, so a large count cannot promote this to ``"dropped"``.
    - ``"off"``: ``had_marker`` is False — every record was examined and no
      ``bridge-session`` marker exists, so Remote Control was never enabled
      on this session. You cannot drop what was never up, so the count is
      irrelevant here too.
    - ``"dropped"``: a marker was seen, and more than ``stale_after``
      records have been written since — the bridge came up and then went
      quiet while claude kept working.
    - ``"ok"``: a marker was seen and the transcript is within the
      threshold.

    The ``None``/``False`` split is #33. Both used to be ``False``, so the
    card asserted ``"off"`` — *Remote Control was never enabled here* —
    about sessions the walk had merely stopped looking at. The same
    distinction ``tmux.list_sessions() -> set | None`` draws for liveness
    (run-2 audit F16): a query that could not answer must not be read as a
    query that answered "no".
    """
    if had_marker is None:
        return "unknown"
    if not had_marker:
        return "off"
    if records_since_marker > stale_after:
        return "dropped"
    return "ok"
