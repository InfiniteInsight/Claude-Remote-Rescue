"""Windows Scheduled Task builder tests (Phase 4). Pure; runs anywhere."""

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
