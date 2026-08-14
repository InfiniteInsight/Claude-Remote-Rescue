"""Windows Update hardening — pure assessment (spec 2026-08-12).

Active hours are the window in which Windows will NOT auto-restart. They
may wrap midnight, and Windows caps the span at 18 hours. Both are easy to
get wrong and neither is visible from the numbers alone.
"""

import pytest

from crr.core.harden import (MAX_ACTIVE_HOURS_SPAN, covers, span_hours,
                             valid_span)


@pytest.mark.parametrize("start,end,expected", [
    (7, 19, 12),      # this host's current setting
    (8, 2, 18),       # wraps midnight, exactly the maximum
    (0, 0, 0),        # degenerate: no window
    (22, 23, 1),
    (23, 1, 2),       # wraps
])
def test_span_handles_midnight_wrap(start, end, expected):
    assert span_hours(start, end) == expected


@pytest.mark.parametrize("start,end,hour,inside", [
    (7, 19, 12, True),
    (7, 19, 3, False),      # the builder's overnight sessions, today
    (8, 2, 1, True),        # wrapped window covers after midnight
    (8, 2, 3, False),
    (8, 2, 23, True),
    (8, 2, 8, True),        # start is inclusive
    (8, 2, 2, False),       # end is exclusive
])
def test_covers_respects_the_wrap_and_the_boundaries(start, end, hour, inside):
    assert covers(start, end, hour) is inside


def test_a_span_over_the_windows_maximum_is_rejected_with_the_reason():
    msg = valid_span(6, 1)   # 19 hours
    assert msg and "18" in msg


def test_a_legal_span_validates():
    assert valid_span(8, 2) is None
    assert MAX_ACTIVE_HOURS_SPAN == 18


@pytest.mark.parametrize("start,end", [(-1, 5), (24, 5), (5, 24), (5, -1)])
def test_hours_outside_0_23_are_rejected(start, end):
    assert valid_span(start, end) is not None
