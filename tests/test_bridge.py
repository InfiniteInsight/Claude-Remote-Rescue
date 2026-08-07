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
