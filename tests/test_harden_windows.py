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


def test_harden_reports_a_restart_that_landed_outside_the_window(monkeypatch, capsys):
    # A clean policy state so the restart-measurement line is the thing
    # under test, not the policy gaps.
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: "2026-08-11 03:12:45 [6008] unexpected shutdown\n")
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out
    assert "restarts outside active hours" in out.lower()
    assert "WARN" in out
    assert "03:12:45" in out


def test_harden_reports_clean_when_no_restart_landed_outside_the_window(monkeypatch, capsys):
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: "2026-08-10 14:03:11 [1074] restart\n")
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out
    assert "restarts outside active hours" in out.lower()
    assert "ok" in out.lower()


def test_harden_reports_unknown_when_events_exist_but_none_parse(monkeypatch, capsys):
    # A format this module does not recognize must render as unknown, not
    # as a manufactured "no restarts outside the window" clean bill.
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: "some unrecognized event shape entirely\n")
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out.lower()
    assert "unkn" in out
    assert "protected" not in out and "guarantee" not in out


def test_apply_commands_target_both_levers_and_are_elevated():
    cmds = __import__("crr.adapters.harden_windows", fromlist=["x"]).apply_commands(8, 2)
    joined = " ".join(" ".join(c) for c in cmds)
    assert "NoAutoRebootWithLoggedOnUsers" in joined
    assert "ActiveHoursStart" in joined and "ActiveHoursEnd" in joined
    # HKLM writes need elevation; without it the write silently fails and
    # crr would report success for a policy it never set.
    assert "RunAs" in joined


def test_apply_commands_propagate_the_elevated_writes_exit_code():
    # Fix round 1, Critical: `Start-Process ... -Wait` alone does not
    # propagate the child's exit code, so a failing `reg.exe` (or a
    # declined UAC prompt) left the outer powershell.exe exiting 0 --
    # `_run_commands` only checks returncode, so "settings applied" could
    # print over an untouched registry. `-PassThru` + `exit $p.ExitCode`
    # (with ErrorActionPreference=Stop covering the declined-UAC case) is
    # what makes a real failure visible to `_run_commands`.
    cmds = __import__("crr.adapters.harden_windows", fromlist=["x"]).apply_commands(8, 2)
    for cmd in cmds:
        script = " ".join(cmd)
        assert "-PassThru" in script
        assert "exit $p.ExitCode" in script
        assert "ErrorActionPreference" in script and "Stop" in script


def test_apply_requires_confirmation_and_runs_nothing_when_declined(monkeypatch, capsys):
    ran = []
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    rc = cli.main(["harden", "--apply"])
    assert rc != 0
    assert ran == [], "wrote to the registry without consent"


def test_apply_refuses_without_a_tty_rather_than_writing_unattended(monkeypatch, capsys):
    ran = []
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["harden", "--apply"]) != 0
    assert ran == []


def test_apply_runs_the_commands_once_confirmed(monkeypatch, capsys):
    ran = []
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["harden", "--apply"]) == 0
    assert ran, "confirmed but wrote nothing"


def test_apply_success_wording_is_pinned_and_backed_by_a_readback(monkeypatch, capsys):
    # Fix round 1, Important 4: nothing pinned the success message's
    # wording. The state here matches what --apply requests (8-2, policy
    # set), so the post-apply readback (Important 2) confirms it stuck.
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["harden", "--apply"]) == 0
    out = capsys.readouterr().out.lower()
    assert "readback" in out and "confirms" in out
    # "protected"/"guarantee" may appear only in the explicit, negated
    # non-claim -- pin that it's the negated form, not a bare claim.
    assert "not a guarantee" in out


def test_apply_reports_plainly_when_the_readback_disagrees(monkeypatch, capsys):
    # Fix round 1, Important 2's whole point: `_run_commands` reporting
    # every argv exited 0 is not proof the registry holds what was
    # requested. Here the (mocked) post-apply readback still shows the
    # pre-apply state, 7-19 with no policy set -- crr must say so plainly,
    # not print an unbacked "applied".
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["harden", "--apply"]) == 0
    out = capsys.readouterr().out.lower()
    assert "did not fully take" in out
    assert "readback" in out
