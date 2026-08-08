"""Bridge-drop predicate tests (pure core, dropped-Remote-Control watchdog).

`bridge_state` is the single gate that decides whether a session's mobile
Remote Control link looks dropped: had a marker ever been seen, and how
many transcript records have piled up since the newest one. Mirrors
`test_takeover.py`'s shape for the sibling predicate.
"""

from crr.core import bridge


def test_had_marker_false_is_off_even_with_a_huge_count():
    # You cannot drop what was never up — the count is irrelevant.
    assert bridge.bridge_state(999_999, False, stale_after=150) == "off"


def test_had_marker_false_is_off_at_zero_count_too():
    assert bridge.bridge_state(0, False, stale_after=150) == "off"


def test_below_stale_after_is_ok():
    assert bridge.bridge_state(149, True, stale_after=150) == "ok"


def test_at_stale_after_is_ok():
    # "more than stale_after" drops — exactly at the threshold is still ok.
    assert bridge.bridge_state(150, True, stale_after=150) == "ok"


def test_above_stale_after_is_dropped():
    assert bridge.bridge_state(151, True, stale_after=150) == "dropped"


def test_zero_records_since_marker_is_ok():
    # The marker is the newest record — the healthy common case.
    assert bridge.bridge_state(0, True, stale_after=150) == "ok"


def test_far_above_stale_after_is_dropped():
    assert bridge.bridge_state(10_000, True, stale_after=150) == "dropped"


# --------------------------------------------------------------------------
# The third answer (#33): "we did not finish looking".
#
# `had_marker=False` used to carry two incompatible meanings — "scanned the
# whole transcript, no marker exists" and "ran out of scan window before
# finding one". The first is a fact; the second is an absence of evidence,
# and collapsing them made the card assert `off` ("Remote Control was never
# enabled") about a session it had merely stopped looking at. `None` is the
# adapter's honest "unknown", and it resolves here to its own state rather
# than to any positive claim. Mirrors `tmux.list_sessions() -> set | None`
# (run-2 audit F16), which drew exactly this distinction for liveness.
# --------------------------------------------------------------------------


def test_had_marker_none_is_unknown():
    assert bridge.bridge_state(0, None, stale_after=150) == "unknown"


def test_had_marker_none_is_unknown_even_past_the_threshold():
    # The count cannot rescue an unknown: without a marker to measure FROM,
    # a large `records_since_marker` says nothing about the bridge. This is
    # the case that must never read as "dropped" — a fabricated drop is what
    # would get a live session restarted.
    assert bridge.bridge_state(10_000, None, stale_after=150) == "unknown"


def test_unknown_is_distinct_from_off():
    # The whole point of the tri-state: these two must not be the same value.
    assert bridge.bridge_state(0, None, stale_after=150) != bridge.bridge_state(
        0, False, stale_after=150
    )
