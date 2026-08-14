"""Windows Update hardening adapter — reads only.

NOTHING in this file may write to the registry or change Windows Update
settings. The apply path is exercised with an injected runner.
"""

import pytest

from crr.adapters.harden_windows import parse_state, read_command, read_state
from crr.core.harden import HardenState


def test_read_command_is_unelevated_powershell():
    argv = read_command()
    assert argv[0] == "powershell.exe"
    assert "-NoProfile" in argv
    # Reading HKLM does not need elevation; asking for it would put a UAC
    # prompt in front of a status command.
    assert not any("RunAs" in a for a in argv)


def test_parse_this_hosts_measured_state():
    # Measured on the builder's machine 2026-08-13.
    text = "policy=absent\nActiveHoursStart=7\nActiveHoursEnd=19\nSmartActiveHoursState=0\n"
    assert parse_state(text) == HardenState(
        policy_set=False, active_start=7, active_end=19, smart_hours=False)


def test_parse_a_set_policy_and_smart_hours():
    text = "policy=1\nActiveHoursStart=8\nActiveHoursEnd=2\nSmartActiveHoursState=1\n"
    assert parse_state(text) == HardenState(
        policy_set=True, active_start=8, active_end=2, smart_hours=True)


def test_policy_present_but_zero_is_not_set():
    text = "policy=0\nActiveHoursStart=8\nActiveHoursEnd=2\nSmartActiveHoursState=0\n"
    assert parse_state(text).policy_set is False


@pytest.mark.parametrize("text", ["", "garbage", "ActiveHoursStart=notanumber\n"])
def test_unparseable_output_is_unknown_not_a_guess(text):
    state = parse_state(text)
    assert state.active_start is None and state.active_end is None


def test_a_failed_command_is_all_unknown():
    def boom(argv, timeout):
        raise OSError("powershell.exe not found")

    state = read_state(timeout=5, run=boom)
    assert state == HardenState(None, None, None, None)


def test_smart_hours_absent_value_is_known_off_not_unknown():
    # The UX key exists but has no SmartActiveHoursState value at all (a
    # real state on builds that predate the feature) -- same "absent means
    # a known False" rule as the policy key, not the same as a read failure.
    text = "policy=absent\nActiveHoursStart=7\nActiveHoursEnd=19\nSmartActiveHoursState=absent\n"
    assert parse_state(text).smart_hours is False


def test_smart_hours_line_missing_entirely_is_unknown():
    text = "policy=absent\nActiveHoursStart=7\nActiveHoursEnd=19\n"
    assert parse_state(text).smart_hours is None
