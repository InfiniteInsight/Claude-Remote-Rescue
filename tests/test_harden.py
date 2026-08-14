"""Windows Update hardening — pure assessment (spec 2026-08-12).

Active hours are the window in which Windows will NOT auto-restart. They
may wrap midnight, and Windows caps the span at 18 hours. Both are easy to
get wrong and neither is visible from the numbers alone.
"""

import pytest

from crr.core.harden import (MAX_ACTIVE_HOURS_SPAN, Finding, HardenState,
                             assess, covers, span_hours, valid_span)


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


def _by_key(findings):
    return {f.key: f for f in findings}


def test_this_hosts_measured_state_reports_both_gaps():
    # Measured 2026-08-13: no AU policy key, hours 7-19, smart hours off.
    state = HardenState(policy_set=False, active_start=7, active_end=19,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["no_auto_reboot"].ok is False
    assert found["active_hours"].ok is False
    # The window is legal, just too narrow — the detail must say WHICH,
    # because "wrong" and "not wide enough" have different fixes.
    assert "7" in found["active_hours"].detail and "19" in found["active_hours"].detail


def test_a_matching_window_and_set_policy_is_clean():
    state = HardenState(policy_set=True, active_start=8, active_end=2,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["no_auto_reboot"].ok is True
    assert found["active_hours"].ok is True


def test_an_unreadable_registry_is_unknown_not_unprotected():
    # The spine rule. "I could not read it" is not "it is not set", and it
    # is certainly not "you are protected".
    state = HardenState(policy_set=None, active_start=None, active_end=None,
                        smart_hours=None)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["no_auto_reboot"].ok is None
    assert found["active_hours"].ok is None
    assert "could not" in found["active_hours"].detail.lower()


def test_smart_active_hours_is_reported_because_it_overrides_the_manual_window():
    # With smart hours ON, Windows picks the window itself and the manual
    # values are not what is in force — reporting them as the truth would
    # be a claim crr cannot make.
    state = HardenState(policy_set=True, active_start=8, active_end=2,
                        smart_hours=True)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["active_hours"].ok is None
    assert "smart" in found["active_hours"].detail.lower()


def test_a_wider_window_than_asked_for_is_not_a_finding():
    # If the user already covers more than crr would set, leave it alone.
    state = HardenState(policy_set=True, active_start=6, active_end=0,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["active_hours"].ok is True


def test_unknown_smart_hours_flag_keeps_active_hours_unknown_too():
    # smart_hours itself is tri-state. If we could not read whether it is
    # on, we cannot vouch that the configured start/end are what's in
    # force either -- same spine rule as an unreadable window, applied to
    # the flag that could silently override it.
    state = HardenState(policy_set=True, active_start=8, active_end=2,
                        smart_hours=None)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["active_hours"].ok is None
    assert "smart" in found["active_hours"].detail.lower()


def test_findings_are_frozen():
    import dataclasses
    f = Finding(key="k", ok=True, detail="d")
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.ok = False
