"""Windows Scheduled Task builder tests (Phase 4). Pure; runs anywhere."""

import subprocess

import pytest

from crr.adapters import scheduled_task as st


def test_interval_minutes_rounds_up_min_one():
    assert st.interval_minutes(30) == 1    # sub-minute -> 1
    assert st.interval_minutes(60) == 1
    assert st.interval_minutes(61) == 2
    assert st.interval_minutes(180) == 3


def test_revive_task_reenters_wsl_and_runs_crr_revive():
    cmd = st.create_revive_task_command("/usr/bin/crr", interval_seconds=120, distro="Ubuntu")
    assert cmd[:4] == ["schtasks.exe", "/Create", "/TN", st.REVIVE_TASK]
    tr = cmd[cmd.index("/TR") + 1]
    assert tr == "wsl.exe --distribution Ubuntu -e /usr/bin/crr revive"
    assert cmd[cmd.index("/MO") + 1] == "2"  # 120s -> 2 minutes
    assert "/SC" in cmd and cmd[cmd.index("/SC") + 1] == "MINUTE"


def test_revive_task_without_distro_omits_the_flag():
    cmd = st.create_revive_task_command("/usr/bin/crr", interval_seconds=30)
    tr = cmd[cmd.index("/TR") + 1]
    assert tr == "wsl.exe -e /usr/bin/crr revive"


def test_web_task_runs_at_logon_on_the_given_port():
    cmd = st.create_web_task_command("/usr/bin/crr", port=8377, distro="Ubuntu")
    tr = cmd[cmd.index("/TR") + 1]
    assert tr == "wsl.exe --distribution Ubuntu -e /usr/bin/crr web --port 8377"
    assert cmd[cmd.index("/SC") + 1] == "ONLOGON"


def test_delete_commands_target_both_tasks():
    cmds = st.delete_task_commands()
    names = {c[c.index("/TN") + 1] for c in cmds}
    assert names == {st.REVIVE_TASK, st.WEB_TASK}
    assert all(c[:2] == ["schtasks.exe", "/Delete"] for c in cmds)


# --- is crr installed as Scheduled Tasks? (#161, I4) -------------------------

def test_query_task_command():
    assert st.query_task_command(st.REVIVE_TASK) == [
        "schtasks.exe", "/Query", "/TN", "CRR Revive"]


def _fake_schtasks(monkeypatch, answers, calls):
    """``answers`` maps a task name to a return code or an exception."""
    monkeypatch.setattr(st.shutil, "which",
                        lambda name: "/mnt/c/Windows/system32/schtasks.exe")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        answer = answers[argv[argv.index("/TN") + 1]]
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(argv, answer, "", "")

    monkeypatch.setattr(st.subprocess, "run", fake_run)


@pytest.mark.real_service_probes
@pytest.mark.parametrize("answers, found", [
    ({"CRR Revive": 0, "CRR Web": 0}, True),
    ({"CRR Revive": 1, "CRR Web": 0}, True),      # a half install still counts
    ({"CRR Revive": 1, "CRR Web": 1}, False),
    # Failures and timeouts are "not found", never a traceback.
    ({"CRR Revive": subprocess.TimeoutExpired("schtasks.exe", 3), "CRR Web": 0}, False),
    ({"CRR Revive": OSError("interop off"), "CRR Web": 0}, False),
])
def test_crr_tasks_installed(monkeypatch, answers, found):
    calls = []
    _fake_schtasks(monkeypatch, answers, calls)
    assert st.crr_tasks_installed(3.0) is found
    assert calls[0][0] == st.query_task_command(st.REVIVE_TASK)
    assert all(0 < kwargs["timeout"] <= 3.0 for _, kwargs in calls)
    assert all(kwargs.get("capture_output") for _, kwargs in calls)


@pytest.mark.real_service_probes
def test_crr_tasks_installed_without_schtasks(monkeypatch):
    monkeypatch.setattr(st.shutil, "which", lambda name: None)
    monkeypatch.setattr(st.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not run schtasks.exe"))
    assert st.crr_tasks_installed(3.0) is False
