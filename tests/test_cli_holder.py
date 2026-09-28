"""`crr holder` — pause / stop / resume the WSL holder task (#138 follow-up).

Every Windows touchpoint is injected: the hold-flag path, the schtasks /
wsl.exe runner, and the Windows boot time. Nothing here ends a real task.
"""

import json

import pytest

from crr import cli
from crr.adapters import boot_windows
from crr.adapters.boot_windows import BootFacts
from crr.core import holder_hold

_BOOT = 1_789_800_000.0


@pytest.fixture
def host(tmp_path, monkeypatch):
    ran = []
    flag = tmp_path / boot_windows.HOLD_FILE_NAME
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_holder_hold_path", lambda: flag)
    monkeypatch.setattr(cli, "_holder_exec", lambda argv: ran.append(argv) or True)
    monkeypatch.setattr(cli, "_holder_running", lambda: True)
    monkeypatch.setattr(cli, "_holder_end_marked", lambda: 1)
    monkeypatch.setattr(cli.boot_windows, "holder_pids", lambda **k: [])
    monkeypatch.setattr(cli.boot_windows, "read_facts",
                        lambda **k: BootFacts(_BOOT, None, None, None, None))
    return flag, ran


def _flag(path):
    return json.loads(path.read_text())


def test_pause_writes_a_timed_hold_and_releases_the_running_holder(host):
    flag, ran = host
    assert cli.main(["holder", "pause", "30"]) == 0
    rec = _flag(flag)
    assert rec["mode"] == "pause" and rec["until"] - rec["set_at"] == 1800
    assert ran == [boot_windows.end_holder_command()]


def test_pause_requires_positive_minutes(host, capsys):
    flag, ran = host
    assert cli.main(["holder", "pause", "0"]) == 2
    assert cli.main(["holder", "pause"]) == 2
    assert not flag.exists() and ran == []


def test_stop_holds_until_resumed(host):
    flag, ran = host
    assert cli.main(["holder", "stop"]) == 0
    assert _flag(flag)["mode"] == "stop"
    assert ran == [boot_windows.end_holder_command()]


def test_stop_until_reboot_records_the_windows_boot(host):
    flag, ran = host
    assert cli.main(["holder", "stop", "--until-reboot"]) == 0
    rec = _flag(flag)
    assert rec["mode"] == "until-reboot" and rec["boot"] == int(_BOOT)


def test_stop_until_reboot_refuses_when_the_boot_time_is_unreadable(host, monkeypatch, capsys):
    flag, ran = host
    monkeypatch.setattr(cli.boot_windows, "read_facts",
                        lambda **k: BootFacts(None, None, None, None, None))
    assert cli.main(["holder", "stop", "--until-reboot"]) == 2
    assert not flag.exists() and ran == []
    assert "boot time" in capsys.readouterr().err


def test_shutdown_flag_shuts_wsl_down_last(host):
    flag, ran = host
    assert cli.main(["holder", "stop", "--shutdown"]) == 0
    assert ran == [boot_windows.end_holder_command(), ["wsl.exe", "--shutdown"]]


def test_resume_clears_the_hold_and_starts_the_holder_now(host):
    flag, ran = host
    cli.main(["holder", "stop"])
    ran.clear()
    assert cli.main(["holder", "resume"]) == 0
    assert not flag.exists()
    assert ran == [boot_windows.run_holder_command()]


def test_resume_without_a_hold_still_starts_the_holder(host):
    flag, ran = host
    assert cli.main(["holder", "resume"]) == 0
    assert ran == [boot_windows.run_holder_command()]


def test_status_reports_the_hold_and_the_task(host, capsys):
    flag, ran = host
    cli.main(["holder", "pause", "15"])
    capsys.readouterr()
    assert cli.main(["holder", "status"]) == 0
    out = capsys.readouterr().out
    assert "paused until" in out
    assert "running" in out


def test_status_is_the_default_action(host, capsys):
    assert cli.main(["holder"]) == 0
    assert "active" in capsys.readouterr().out


def test_holder_refuses_outside_wsl(host, monkeypatch, capsys):
    flag, ran = host
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    assert cli.main(["holder", "stop"]) == 2
    assert not flag.exists() and ran == []


def test_the_marked_holder_is_ended_even_when_schtasks_end_fails(host, monkeypatch):
    # With the launcher, /end only kills powershell.exe; ending the marked
    # Linux-side holder is what actually releases WSL.
    flag, _ = host
    monkeypatch.setattr(cli, "_holder_exec", lambda argv: False)
    assert cli.main(["holder", "stop"]) == 0


def test_a_failed_release_is_reported_not_hidden(host, monkeypatch, capsys):
    flag, _ = host
    monkeypatch.setattr(cli, "_holder_exec", lambda argv: False)
    monkeypatch.setattr(cli, "_holder_end_marked", lambda: 0)
    assert cli.main(["holder", "stop"]) == 1
    # The hold is still written: the next re-arm will respect it even though
    # the running holder couldn't be ended now.
    assert _flag(flag)["mode"] == "stop"
    assert "could not end" in capsys.readouterr().err


def test_an_unwritable_flag_location_is_a_clean_error(host, monkeypatch, capsys):
    flag, ran = host

    def boom(path, obj):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(cli, "write_json_atomic", boom)
    assert cli.main(["holder", "stop"]) == 1
    assert "could not write the hold flag" in capsys.readouterr().err
    assert ran == []  # never end the holder without a hold in place
