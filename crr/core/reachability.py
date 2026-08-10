"""Is this session reachable from the phone, and may it be restarted?
(spec 2026-08-09, Phases 1-2 — replaces the record-counting detector.)

Pure core: two predicates over facts an adapter already sampled from
Claude Code's own per-process state file. No I/O, no clock — mirrors
``crr.core.takeover.ready_to_take_over``'s shape.

Why this replaced counting transcript records: the old detector needed a
median of 8 minutes of ACTIVE work to fire (measured across 93 transcripts
/ 3,659 continuously-active windows) and never fired at all on an idle
session, because an idle session writes no records. That is precisely the
session the feature exists for — the one sitting disconnected while its
owner is away from the keyboard. The old design treated it as a false
positive; it is the case.
"""

from __future__ import annotations

REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

# Claude Code's own `status` values, observed on disk. `waiting` carries a
# `waitingFor` describing what it is blocked on ("permission prompt",
# "input needed").
_KICKABLE = ("idle", "waiting")
_BUSY = ("busy", "shell")


def reachability(
    bridge_session_id: str | None, *, pid_matched: bool, field_present: bool,
) -> str:
    """Classify the phone link as reachable / unreachable / unknown.

    Every failure route lands on ``unknown``; none may produce a positive
    claim. In order:

    - ``pid_matched`` False — the newest state file for this session id
      belongs to a pid that is NOT one of this session's live claude
      processes. It is a leftover from a dead process, or worse a RECYCLED
      pid now owned by something unrelated. Measured on the author's
      machine: 117 of 133 state files had dead pids and 2 had recycled
      ones, and one session had three files with two "alive" pids. A
      liveness check alone returns a confident wrong answer.
    - ``field_present`` False — the file carries no ``bridgeSessionId`` key
      at all (an older Claude Code, or a renamed field). Absence of the
      field is not evidence the bridge is down.
    - otherwise the id itself decides. Falsy (``None``, and ``""`` for
      safety) is ``unreachable``; anything else is ``reachable``.
    """
    if not pid_matched or not field_present:
        return UNKNOWN
    return REACHABLE if bridge_session_id else UNREACHABLE


def may_kick(status: str | None) -> tuple[bool, str]:
    """Does this session's reported activity permit restarting it?

    ``(True, "")`` when it does; ``(False, <human reason>)`` when it does
    not. Kicking destroys whatever turn is in flight, so the two working
    states are hard blocks:

    - ``busy``    — claude is generating. This is where work dies.
    - ``shell``   — a command is running under it.
    - ``idle``    — a completed turn, nothing in flight. Safe.
    - ``waiting`` — blocked on the USER (a permission prompt, or a question).
      Safe, and the important one: such a session never reaches a clean
      assistant-end turn boundary, so a boundary-only guard would refuse it
      forever — leaving it stuck on a question its owner cannot answer,
      because the phone is disconnected. Restarting loses at most one
      pending tool call; the conversation resumes intact.

    Anything unrecognised — including ``None`` and a status a future Claude
    Code invents — is refused. An unreadable signal is not a licence to
    signal a live process.
    """
    if status in _KICKABLE:
        return True, ""
    if status in _BUSY:
        return False, f"session is {status} — work in flight"
    return False, f"unknown activity status {status!r} — refusing to kick"
