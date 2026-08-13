"""Power-block decision (spec 2026-08-12) — pure, no I/O.

The decision is separated from the holding so every reason to NOT hold is
testable without a platform. `withheld` exists because "crr is not holding
anything" is useless to a user without the reason.
"""

from crr.core.power import Decision, decide, unmet


def test_off_holds_nothing():
    d = decide(live_sessions=3, on_ac=True, mode="off", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "off" in d.withheld


def test_no_live_session_holds_nothing():
    d = decide(live_sessions=0, on_ac=True, mode="sleep", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "no live" in d.withheld


def test_sleep_mode_holds_only_sleep():
    d = decide(live_sessions=1, on_ac=True, mode="sleep", requires_ac=True)
    assert d.want == frozenset({"sleep"})
    assert d.withheld is None


def test_sleep_plus_shutdown_holds_both():
    d = decide(live_sessions=1, on_ac=True, mode="sleep+shutdown",
               requires_ac=True)
    assert d.want == frozenset({"sleep", "shutdown"})


def test_on_battery_withholds_when_ac_is_required():
    d = decide(live_sessions=2, on_ac=False, mode="sleep", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "battery" in d.withheld


def test_on_battery_holds_when_ac_is_not_required():
    d = decide(live_sessions=2, on_ac=False, mode="sleep", requires_ac=False)
    assert d.want == frozenset({"sleep"})


def test_unknown_power_source_withholds_rather_than_guessing():
    # Spine principle: an unknown must never become a positive claim in
    # EITHER direction. "I could not read the power source" is not "on AC".
    d = decide(live_sessions=2, on_ac=None, mode="sleep", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "cannot tell" in d.withheld


def test_unknown_power_source_is_irrelevant_when_ac_is_not_required():
    d = decide(live_sessions=2, on_ac=None, mode="sleep", requires_ac=False)
    assert d.want == frozenset({"sleep"})


def test_an_unrecognised_mode_holds_nothing_and_says_so():
    # A typo in config.toml must not silently disable protection the user
    # thinks they enabled, nor crash the poll loop.
    d = decide(live_sessions=1, on_ac=True, mode="slep", requires_ac=True)
    assert d.want == frozenset()
    assert d.withheld and "slep" in d.withheld


def test_reason_names_the_session_count_for_the_os_blocking_ui():
    d = decide(live_sessions=3, on_ac=True, mode="sleep", requires_ac=True)
    assert "3" in d.reason and "Claude" in d.reason


def test_decision_is_frozen():
    import dataclasses
    import pytest
    d = decide(live_sessions=1, on_ac=True, mode="sleep", requires_ac=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.want = frozenset()


def test_unmet_is_empty_when_the_platform_can_do_it_all():
    assert unmet(frozenset({"sleep", "shutdown"}),
                 frozenset({"sleep", "shutdown"})) == ()


def test_unmet_names_what_this_platform_cannot_deliver():
    # macOS: caffeinate holds sleep, nothing holds shutdown. Doctor must
    # say so rather than silently holding half of what was asked.
    assert unmet(frozenset({"sleep"}),
                 frozenset({"sleep", "shutdown"})) == ("shutdown",)


def test_unmet_is_sorted_so_output_is_stable():
    assert unmet(frozenset(), frozenset({"shutdown", "sleep"})) == (
        "shutdown", "sleep")


def test_ports_declare_the_power_protocols():
    # The adapters in later tasks are checked against these signatures;
    # a rename here without a rename there is a silent breakage, because
    # Protocols are structural and nothing fails at import time.
    import inspect
    from crr.core import ports
    assert hasattr(ports, "PowerSource")
    assert hasattr(ports, "PowerHolder")
    assert list(inspect.signature(ports.PowerHolder.hold).parameters) == [
        "self", "want", "reason"]
    assert list(inspect.signature(ports.PowerSource.on_ac).parameters) == [
        "self"]
