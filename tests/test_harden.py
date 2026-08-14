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


def test_a_same_span_window_shifted_earlier_still_leaves_a_gap():
    # Fix round 1: this used to assert ok is True on the theory that a
    # window starting earlier "already covers more than crr would set."
    # It doesn't. 6:00-00:00 covers hours 06..23; 00:00-02:00 is NOT in
    # it. want=(8,2) is 18h, the Windows cap, so no legal window strictly
    # contains it except itself -- no containment predicate can ever
    # return True here, and none should: 00:00-02:00 is genuinely
    # unprotected and the finding must say so, by name.
    state = HardenState(policy_set=True, active_start=6, active_end=0,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["active_hours"].ok is False
    assert "00:00-02:00" in found["active_hours"].detail


def test_a_window_that_genuinely_contains_the_wanted_one_is_clean():
    # want=(9,1) is narrower than the Windows 18h cap, so an existing
    # window can actually strictly contain it: 8:00-02:00 covers
    # 09:00-01:00 in full.
    state = HardenState(policy_set=True, active_start=8, active_end=2,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=9, want_end=1))
    assert found["active_hours"].ok is True


@pytest.mark.parametrize("active_start,active_end,expected_ok,expected_gap_hours", [
    (0, 18, False, 6),    # 18:00-00:00 uncovered = 6h (daily recurrence covers 00-01 too)
    (2, 20, False, 6),    # 20:00-02:00 uncovered = 6h
    (20, 14, False, 6),   # 14:00-20:00 uncovered = 6h
    (9, 3, False, 1),     # 08:00-09:00 uncovered = 1h
])
def test_the_verdict_tracks_actual_exposure_not_window_shape(
        active_start, active_end, expected_ok, expected_gap_hours):
    # The whole point of the fix round: a containment predicate can be
    # non-monotonic in the real gap (the review's example: green over a
    # 6-hour uncovered window, red over a 1-hour one). This pins that
    # more uncovered hours is never "greener" than fewer, by checking the
    # actual gap size against want=(8,2).
    state = HardenState(policy_set=True, active_start=active_start,
                        active_end=active_end, smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["active_hours"].ok is expected_ok
    from crr.core.harden import _uncovered_ranges, span_hours
    gaps = _uncovered_ranges(active_start, active_end, 8, 2)
    total_gap = sum(span_hours(s, e) for s, e in gaps)
    assert total_gap == expected_gap_hours


@pytest.mark.parametrize("want_start,want_end", [(8, 2), (9, 1), (7, 19)])
def test_ok_is_true_exactly_when_nothing_is_uncovered(want_start, want_end):
    # Exhaustive version of the monotonicity check: for every legal
    # (start, end) pair, ok must be True precisely when there are zero
    # uncovered hours -- never green over any real gap, never red when
    # the window is genuinely clean. This makes the earlier bug (a
    # containment predicate that was True over a 6h gap and False over a
    # 1h one) structurally impossible rather than checked at a few points.
    from crr.core.harden import _uncovered_ranges
    for active_start in range(24):
        for active_end in range(24):
            state = HardenState(policy_set=True, active_start=active_start,
                                active_end=active_end, smart_hours=False)
            found = _by_key(assess(state, want_start=want_start, want_end=want_end))
            gaps = _uncovered_ranges(active_start, active_end, want_start, want_end)
            assert found["active_hours"].ok is (not gaps), (
                f"active=({active_start},{active_end}) want=({want_start},"
                f"{want_end}) gaps={gaps} ok={found['active_hours'].ok}")


def test_two_separate_gaps_are_both_named():
    # The only nontrivial branch of _uncovered_ranges: a middle chunk of
    # the wanted window is covered, leaving a gap at each end. Both must
    # be named, not just the first.
    state = HardenState(policy_set=True, active_start=10, active_end=14,
                        smart_hours=False)
    found = _by_key(assess(state, want_start=8, want_end=2))
    assert found["active_hours"].ok is False
    detail = found["active_hours"].detail
    assert "08:00-10:00" in detail
    assert "14:00-02:00" in detail


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


from crr.core.harden import restarts_outside


def test_a_restart_inside_the_window_is_not_evidence_of_failure():
    events = ["2026-08-10 14:03:11 [1074] The process ... initiated the restart"]
    assert restarts_outside(events, start=8, end=2) == ()


def test_a_restart_outside_the_window_is_reported():
    # 03:12 with an 08:00-02:00 window: the hardening did not hold.
    events = ["2026-08-11 03:12:45 [6008] The previous system shutdown was unexpected"]
    out = restarts_outside(events, start=8, end=2)
    assert len(out) == 1 and "03:12" in out[0]


def test_an_unparseable_event_line_is_skipped_not_counted_either_way():
    # Cannot tell when it happened -> cannot claim it broke the window, and
    # cannot claim it did not.
    assert restarts_outside(["no timestamp here"], start=8, end=2) == ()


from datetime import datetime

from crr.core.harden import within_lookback


def test_within_lookback_keeps_events_inside_the_window():
    now = datetime(2026, 8, 13, 12, 0, 0)
    events = ["2026-08-10 14:03:11 [1074] recent"]
    assert within_lookback(events, days=14, now=now) == tuple(events)


def test_within_lookback_drops_events_older_than_the_window():
    now = datetime(2026, 8, 13, 12, 0, 0)
    events = ["2026-07-01 14:03:11 [1074] stale"]
    assert within_lookback(events, days=14, now=now) == ()


def test_within_lookback_skips_unparseable_lines_rather_than_guessing_recency():
    now = datetime(2026, 8, 13, 12, 0, 0)
    assert within_lookback(["no timestamp here"], days=14, now=now) == ()


def test_a_restart_outside_the_window_is_reported_in_us_date_format():
    # PowerShell's default TimeCreated.ToString() is culture-dependent.
    # Measured on a real WSL/Windows host: "MM/dd/yyyy HH:mm:ss", not ISO.
    # Trusting only ISO would parse zero timestamps there and print a
    # false-clean "no restarts outside the window".
    events = ["08/09/2026 03:12:45 [6008] The previous system shutdown was unexpected"]
    out = restarts_outside(events, start=8, end=2)
    assert len(out) == 1 and "03:12" in out[0]


from crr.core.harden import parsed_count


def test_parsed_count_distinguishes_no_events_from_unparseable_events():
    assert parsed_count([]) == 0
    assert parsed_count(["garbage", "Reason Code: 0x0"]) == 0
    assert parsed_count(["2026-08-10 14:03:11 [1074] restart", "garbage"]) == 1
