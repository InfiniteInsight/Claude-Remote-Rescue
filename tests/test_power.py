"""Power-block decision (spec 2026-08-12) — pure, no I/O.

The decision is separated from the holding so every reason to NOT hold is
testable without a platform. `withheld` exists because "crr is not holding
anything" is useless to a user without the reason.
"""

from crr.core.power import (Decision, POWER_SNAPSHOT_VERSION, Report, decide,
                            interpret, snapshot, unmet)


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


# --- cross-process visibility: snapshot() / interpret() (fix round 1, ------
# --- 2026-08-13) -------------------------------------------------------
#
# `crr power`/`crr doctor` run in a process separate from `crr awake` and
# have no handle to what its holder holds -- constructing a fresh holder
# and asking `.held()` measured as the actual bug (a real `crr awake`
# holding, sampled from a separate `crr power`, reported "holding:
# nothing"). These tests cover the PURE interpretation half of the fix:
# turning a raw (possibly absent/orphaned/stale) snapshot dict into an
# honest Report, with no I/O and no clock of its own.

def test_snapshot_is_json_shaped_and_versioned():
    data = snapshot(frozenset({"sleep"}), "crr: 1 Claude session live", 4242, 1000.0)
    assert data["v"] == POWER_SNAPSHOT_VERSION
    assert data["held"] == ["sleep"]  # a sorted list, not a frozenset -- JSON has no set type
    assert data["reason"] == "crr: 1 Claude session live"
    assert data["pid"] == 4242
    assert data["updated"] == 1000.0


def test_interpret_missing_file_is_a_known_nothing_not_an_unknown():
    # No loop has EVER reported -- a fact, not a guess.
    r = interpret(None, now=1000.0, pid_alive=False, max_age_seconds=90)
    assert r == Report(frozenset(), "no keep-awake loop has reported", None)


def test_interpret_dead_writer_is_unknown_never_nothing():
    # The exact conflation this fix exists to end: a dead writer's last
    # claim must not be read as "released".
    data = snapshot(frozenset({"sleep"}), "crr: 1 Claude session live", 4242, 1000.0)
    r = interpret(data, now=1001.0, pid_alive=False, max_age_seconds=90)
    assert r.unknown is not None
    assert r.held == frozenset()  # never assert what a dead writer claimed
    assert r.reason is None


def test_interpret_stale_timestamp_is_unknown_never_nothing():
    # A wedged loop (hung, blocked) stops polling without dying -- a live
    # pid alone is not proof the last claim still holds.
    data = snapshot(frozenset({"sleep"}), "crr: 1 Claude session live", 4242, 1000.0)
    r = interpret(data, now=1000.0 + 200, pid_alive=True, max_age_seconds=90)
    assert r.unknown is not None
    assert "200" in r.unknown
    assert r.held == frozenset()


def test_interpret_fresh_alive_report_is_trusted():
    data = snapshot(frozenset({"sleep"}), "crr: 1 Claude session live", 4242, 1000.0)
    r = interpret(data, now=1005.0, pid_alive=True, max_age_seconds=90)
    assert r == Report(frozenset({"sleep"}), "crr: 1 Claude session live", None)


def test_interpret_fresh_alive_report_with_nothing_held_is_trusted_too():
    # A live, current report saying "nothing" IS a positive claim (crr
    # really is holding nothing right now) -- distinct from `unknown`.
    data = snapshot(frozenset(), "power_block is off", 4242, 1000.0)
    r = interpret(data, now=1005.0, pid_alive=True, max_age_seconds=90)
    assert r == Report(frozenset(), "power_block is off", None)


def test_interpret_exactly_at_the_age_boundary_is_still_trusted():
    # age == max_age_seconds is not YET stale -- only strictly older is.
    data = snapshot(frozenset({"sleep"}), "crr: 1 Claude session live", 4242, 1000.0)
    r = interpret(data, now=1090.0, pid_alive=True, max_age_seconds=90)
    assert r.unknown is None
    assert r.held == frozenset({"sleep"})
