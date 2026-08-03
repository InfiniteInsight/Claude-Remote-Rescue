"""Takeover-readiness predicate (`crr adopt --takeover`).

Pure core: decides WHETHER a live session's transcript is safe to stop
and adopt, given two already-sampled facts (idle seconds, tail turn kind)
and a configured idle window. No I/O, no clock, no sleep — the cli owns
the poll loop and the wall-clock reads; this module only judges.

Why only an ``"assistant-end"`` tail is safe: Claude Code always emits an
assistant turn in reply to a user prompt, so on a *live* session a tail of
``"user-prompt"`` means a response is still pending (about to stream, or
mid non-streaming API call) — killing there loses the in-flight reply.
``"mid-turn"`` (a tool round-trip still running) is unsafe by definition.
An assistant record with ``stop_reason == "end_turn"`` at the tail is
precisely the common "finished, awaiting the user" idle state — the one
point in a live conversation where nothing is in flight. See
``crr.core.transcript.turn_boundary`` for how a single record is
classified into one of the four kinds this predicate consumes.
"""

from __future__ import annotations


def ready_to_take_over(seconds_idle: float, tail_kind: str, *, idle_window: float) -> bool:
    """True iff the transcript has been idle >= ``idle_window`` AND its
    newest turn-bearing record is a clean ``"assistant-end"`` boundary.

    Any other ``tail_kind`` (``"user-prompt"``, ``"mid-turn"``, ``"other"``,
    or an empty string for an absent/unreadable transcript) is never
    ready, no matter how long ``seconds_idle`` is — mtime alone is not
    proof of idleness (a long non-streaming completion can leave the file
    quiet mid-turn), so the tail-kind check is load-bearing, not a
    formality.
    """
    return seconds_idle >= idle_window and tail_kind == "assistant-end"
