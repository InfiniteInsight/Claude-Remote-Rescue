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


from crr import cli
from crr.core.harden import HardenState as _HS


def _patch_state(monkeypatch, state):
    monkeypatch.setattr(cli.harden_windows, "read_state", lambda **k: state)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)


def test_harden_reports_the_gaps_and_the_command_that_fixes_them(monkeypatch, capsys):
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out
    assert "NoAutoRebootWithLoggedOnUsers" in out
    assert "crr harden --apply" in out


def test_harden_says_applied_never_protected(monkeypatch, capsys):
    # Microsoft filed these under "Legacy Policies" and they are reported
    # ignored in the wild. crr may claim it applied a setting; it may never
    # claim the machine is safe.
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    cli.main(["harden"])
    out = capsys.readouterr().out.lower()
    assert "protected" not in out
    assert "guarantee" not in out


def test_harden_reports_unknown_rather_than_unprotected(monkeypatch, capsys):
    _patch_state(monkeypatch, _HS(None, None, None, None))
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out.lower()
    assert "unknown" in out or "could not" in out


def test_harden_refuses_on_a_host_with_no_windows_to_harden(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    rc = cli.main(["harden"])
    assert rc != 0
    assert "windows" in capsys.readouterr().err.lower()


def test_doctor_carries_the_same_finding(monkeypatch, capsys):
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: __import__("pathlib").Path("/tmp"))
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "windows update" in out.lower()
