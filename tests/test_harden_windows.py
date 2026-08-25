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


def _no_safety_claim(out_lower: str) -> bool:
    """True when no "is protected"/"are protected" SAFETY CLAIM appears.

    A blunt ``"protected" not in out`` (fix rounds 1-2) only ever passed
    because its fixtures happened to avoid the gap/unknown path -- it
    never actually distinguished an honest gap sentence from a dishonest
    claim, and would keep passing even if crr started printing "you are
    now protected" right next to an honest "hours not covered" gap. This
    checks the actual rule: crr may say "applied", never that the machine
    "is protected" or "are protected".
    """
    return "is protected" not in out_lower and "are protected" not in out_lower


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
    assert _no_safety_claim(out)
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
    # under test, not the policy gaps. The restart date is relative to now
    # (yesterday) rather than a fixed calendar date, so this test does not
    # rot once "now" drifts past the fixed date's 14-day lookback window.
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: f"{recent} 03:12:45 [6008] unexpected shutdown\n")
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
    assert _no_safety_claim(out)


# --- Critical 1 (fix round 3): efficacy must be measured against the hours
# actually IN FORCE (state.active_start/active_end, read from the
# registry), never the configured want-window -- a restart that lands
# inside the configured window but outside what is really in force is
# exactly the failure this measurement exists to catch, and measuring
# against the want-window greens it. crr's config here defaults to 8-2;
# these fixtures deliberately give the in-force window a DIFFERENT value
# (7-19) so a test that silently kept reading want_start/want_end would
# fail loudly instead of passing by coincidence.

def test_restart_measurement_uses_in_force_hours_not_the_configured_window(monkeypatch, capsys):
    # 01:14 is INSIDE the configured want-window (8-2) but OUTSIDE the
    # in-force window (7-19) -- the exact shape that motivated this fix.
    _patch_state(monkeypatch, _HS(True, 7, 19, False))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: "2026-08-14 01:14:24 [6008] unexpected shutdown\n")
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out
    assert "restarts outside active hours" in out.lower()
    assert "WARN" in out
    assert "01:14:24" in out
    assert "7:00-19:00" in out or "07:00-19:00" in out


def test_restart_measurement_same_restart_not_outside_a_different_in_force_window(monkeypatch, capsys):
    # Same 01:14 restart, but here the in-force window IS 8-2 -- 01:14 is
    # inside it, so this is not evidence of failure.
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: "2026-08-14 01:14:24 [6008] unexpected shutdown\n")
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out
    assert "restarts outside active hours" in out.lower()
    assert "ok" in out.lower()
    assert "WARN" not in out


def test_restart_measurement_is_unknown_when_in_force_hours_are_unreadable(monkeypatch, capsys):
    _patch_state(monkeypatch, _HS(True, None, None, False))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: "2026-08-14 01:14:24 [6008] unexpected shutdown\n")
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "restarts outside active hours" in l.lower())
    assert "unkn" in line.lower()
    assert "[ok" not in line.lower() and "warn" not in line.lower()


def test_restart_measurement_is_unknown_when_smart_hours_is_on(monkeypatch, capsys):
    # smart_hours True means Windows picks the window itself -- crr does
    # not know what is actually in force, so it cannot measure against it.
    _patch_state(monkeypatch, _HS(True, 8, 2, True))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: "2026-08-14 01:14:24 [6008] unexpected shutdown\n")
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out.lower()
    assert "restarts outside active hours" in out
    assert "unkn" in out
    assert "smart active hours" in out


def test_restart_measurement_is_unknown_when_smart_hours_is_unreadable(monkeypatch, capsys):
    # Deliberate consistency extension beyond the literal ruling text: when
    # crr cannot tell whether smart hours overrides the configured window
    # (smart_hours is None), it cannot vouch that active_start/active_end
    # are what is really in force either -- the identical unknown
    # harden.assess() already renders for this same state (harden.py's
    # active_hours finding). Measuring restarts against a window crr
    # cannot confirm is live would contradict that unknown on the very
    # same report.
    _patch_state(monkeypatch, _HS(True, 8, 2, None))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: "2026-08-14 01:14:24 [6008] unexpected shutdown\n")
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out.lower()
    assert "restarts outside active hours" in out
    assert "unkn" in out


# --- Minor 2 (fix round 3): the parsed_count honesty gate must look at
# whether the RECENT window parsed, not whether anything in the whole,
# pre-lookback event list happened to parse -- an old parseable line must
# not paper over an entirely-unparseable recent window.

def test_restart_measurement_unknown_when_only_an_ancient_line_parses(monkeypatch, capsys):
    # One very old, perfectly parseable restart, plus lines this module
    # cannot read at all. The old naive gate (parsed_count over the full,
    # pre-lookback list) saw the one parseable line and let the report
    # through to a manufactured "no restarts outside the window" -- the
    # unparseable lines, which within_lookback() also silently drops
    # (correctly -- it cannot vouch for their recency), simply vanished.
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(
        cli, "run_capture",
        lambda cmd, timeout: (
            "2020-01-01 00:00:00 [1074] a restart from years ago\n"
            "some unrecognized recent event shape\n"
            "another unrecognized recent event shape\n"
        ))
    assert cli.main(["harden"]) == 0
    out = capsys.readouterr().out.lower()
    assert "restarts outside active hours" in out
    assert "unkn" in out
    assert "no restarts" not in out


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


def test_apply_commands_guard_a_null_process_handle_before_reading_exitcode():
    # Fix round 2, residual: whether `ErrorActionPreference = 'Stop'`
    # actually promotes a declined-UAC prompt's non-terminating exception
    # into a terminating one is untestable without a real UAC prompt --
    # and if it does not, `$p` is `$null` and `exit $p.ExitCode` is
    # `exit $null`, which PowerShell treats as `exit 0`: the exact bug
    # the exit-code fix exists to close, reappearing one branch over.
    # Guard the null case explicitly so correctness does not depend on
    # that untested promotion.
    cmds = __import__("crr.adapters.harden_windows", fromlist=["x"]).apply_commands(8, 2)
    for cmd in cmds:
        script = " ".join(cmd)
        assert "if (-not $p) { exit 1 }" in script
        # the guard must run before the unconditional exit, or it can't help
        assert script.index("if (-not $p)") < script.index("exit $p.ExitCode")


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
    # State matches what --apply requests (8-2, policy set), so the
    # post-apply readback (Important 2, fix round 1) confirms it stuck and
    # rc stays 0 -- this test is about the confirmation gate letting the
    # commands run at all, not about the readback verdict, so the fixture
    # state must not itself be a disagreement (see fix round 2: a mismatched
    # readback is now a nonzero rc, covered separately below).
    ran = []
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["harden", "--apply"]) == 0
    assert ran, "confirmed but wrote nothing"


# Hardcoded literals, deliberately NOT read back from `cli._APPLY_*` --
# fix round 2, Important 4 note on the first attempt at this test: asserting
# `cli._APPLY_CONFIRMED_LINE in out` is not a pin at all, because the
# printed text and the expected text are the same mutable reference. If
# someone mutates the constant in crr/cli.py to a dishonest sentence, that
# assertion mutates right along with it and still passes. Pinning the exact
# wording here, independent of the source, is what makes a dishonest edit
# to the success line actually fail a test. (Self-verified: temporarily
# mutating _APPLY_CONFIRMED_LINE in crr/cli.py to a "your machine is now
# protected ..." sentence and rerunning this file's apply tests reproduced
# exactly one failure, here, before the constant was reverted.)
_APPLY_CONFIRMED_LINE = (
    "crr harden --apply: wrote the settings; a readback of the registry "
    "confirms both levers now match what was requested.")
_APPLY_DISAGREE_HEADLINE = (
    "crr harden --apply: wrote the settings, but a readback of the "
    "registry shows they did NOT take:")
_APPLY_UNKNOWN_HEADLINE = (
    "crr harden --apply: wrote the settings, but the registry could not "
    "be read back afterward to confirm they took:")


def test_apply_success_wording_is_pinned_and_backed_by_a_readback(monkeypatch, capsys):
    # Fix round 1, Important 4 (NOT addressed the first time: the original
    # version of this test only checked keywords over whole stdout, which
    # a mutated, "your machine is now protected" success line still
    # passed). Assert the exact success sentence itself, hardcoded above
    # rather than imported from crr.cli. The state here matches what
    # --apply requests (8-2, policy set), so the post-apply readback
    # (Important 2) confirms it stuck.
    _patch_state(monkeypatch, _HS(True, 8, 2, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["harden", "--apply"]) == 0
    out = capsys.readouterr().out
    assert _APPLY_CONFIRMED_LINE in out
    # "protected"/"guarantee" may appear only in the explicit, negated
    # non-claim -- pin that it's the negated form, not a bare claim.
    assert "This is not a guarantee the machine is protected" in out


def test_apply_returns_nonzero_and_says_so_plainly_when_the_readback_disagrees(monkeypatch, capsys):
    # Fix round 2, NEW IMPORTANT: `_run_commands` reporting every argv
    # exited 0 is not proof the registry holds what was requested. Here
    # the (mocked) post-apply readback still shows the pre-apply state,
    # 7-19 with no policy set -- crr must say so plainly AND return
    # nonzero, not print an unbacked "applied" over `&& echo ok`.
    _patch_state(monkeypatch, _HS(False, 7, 19, False))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    rc = cli.main(["harden", "--apply"])
    assert rc != 0, "readback disagreed but exit code still claimed success"
    out = capsys.readouterr().out
    assert _APPLY_DISAGREE_HEADLINE in out
    assert _APPLY_CONFIRMED_LINE not in out
    # Rerunning --apply is not a fix for a write that already ran and
    # didn't stick -- must not suggest it here.
    assert "fix with: crr harden --apply" not in out


def test_apply_readback_unknown_is_not_the_same_headline_as_disagree(monkeypatch, capsys):
    # Fix round 2, NEW IMPORTANT sub-clause: when read_state fails
    # entirely, both findings are ok=None -- "could not read it back" is a
    # weaker, different claim than "confirmed it did not take", and must
    # not share the disagreement branch's headline (null-results rule
    # applied to crr's own write path).
    _patch_state(monkeypatch, _HS(None, None, None, None))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    rc = cli.main(["harden", "--apply"])
    assert rc != 0
    out = capsys.readouterr().out
    assert _APPLY_UNKNOWN_HEADLINE in out
    assert _APPLY_DISAGREE_HEADLINE not in out
    assert _APPLY_CONFIRMED_LINE not in out
