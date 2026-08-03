"""Takeover-readiness predicate tests (pure core, adopt --takeover).

`ready_to_take_over` is the single gate that decides whether a live
session's transcript is safe to stop and adopt: idle long enough AND the
tail is the one turn boundary that means "finished, awaiting the user"
(`"assistant-end"` — see crr.core.transcript.turn_boundary). Every other
tail kind means a response or tool round-trip may still be in flight, so
it must never be ready regardless of how long the transcript has looked
idle (mtime can go quiet mid-turn during a long non-streaming call).
"""

from crr.core import takeover


def test_below_idle_window_is_not_ready_even_at_assistant_end():
    assert takeover.ready_to_take_over(11.9, "assistant-end", idle_window=12.0) is False


def test_at_idle_window_with_assistant_end_is_ready():
    assert takeover.ready_to_take_over(12.0, "assistant-end", idle_window=12.0) is True


def test_above_idle_window_with_assistant_end_is_ready():
    assert takeover.ready_to_take_over(999.0, "assistant-end", idle_window=12.0) is True


def test_user_prompt_tail_is_never_ready():
    # Claude Code always emits an assistant turn after a user prompt — a
    # "user-prompt" tail on a live session means the response is still
    # pending, no matter how idle the transcript looks.
    assert takeover.ready_to_take_over(999.0, "user-prompt", idle_window=12.0) is False


def test_mid_turn_tail_is_never_ready():
    assert takeover.ready_to_take_over(999.0, "mid-turn", idle_window=12.0) is False


def test_other_tail_is_never_ready():
    assert takeover.ready_to_take_over(999.0, "other", idle_window=12.0) is False


def test_empty_tail_kind_is_never_ready():
    assert takeover.ready_to_take_over(999.0, "", idle_window=12.0) is False
