"""reachable-at-boot adapters — generation asserted as text; NO registration.

Nothing here registers a Scheduled Task, writes the registry, or reboots. The
elevated register path is exercised with an injected runner in the CLI tests.
"""

import crr.adapters.boot_windows as boot_windows
from crr.adapters.boot_windows import (TAILNET_TASK, WSL_BOOT_TASK,
                                       BootFacts, install_script,
                                       parse_epoch, read_facts,
                                       tailnet_script, wsl_boot_argument,
                                       facts_command)


def test_wsl_boot_argument_holds_the_vm_open():
    arg = wsl_boot_argument("Ubuntu-24.04", "evan")
    # -d <distro> -u <user>, and the keepalive that defeats WSL's ~60s idle
    # shutdown (otherwise the VM boots and dies before you'd notice).
    assert "-d Ubuntu-24.04" in arg
    assert "-u evan" in arg
    assert "exec sleep infinity" in arg


def test_tailnet_script_retries_until_tailscaled_is_ready():
    s = tailnet_script("infiniteinsight@gmail.com")
    assert "switch infiniteinsight@gmail.com" in s
    # A plain switch at boot races tailscaled; the script must retry.
    assert "for" in s.lower() and "start-sleep" in s.lower()


def test_install_script_registers_both_tasks_with_the_right_shape():
    s = install_script("Ubuntu-24.04", "evan", "infiniteinsight@gmail.com",
                       r"C:\ProgramData\crr\tailnet-default.ps1")
    assert f"Register-ScheduledTask" in s
    assert WSL_BOOT_TASK in s and TAILNET_TASK in s
    # S4U: no stored password (PIN login), and it kept the desktop locked.
    assert "S4U" in s
    assert "AtStartup" in s
    assert "Highest" in s
    # unbounded so the sleep-infinity keepalive is not killed after 3 days
    assert "New-TimeSpan" in s or "[TimeSpan]::Zero" in s


def test_install_script_omits_the_tailnet_task_when_no_preference():
    # Single-account host: no tailnet task, no switch script.
    s = install_script("Ubuntu-24.04", "evan", None, r"C:\ProgramData\crr\x.ps1")
    assert WSL_BOOT_TASK in s
    assert TAILNET_TASK not in s


def test_parse_epoch_reads_a_numeric_line_and_rejects_junk():
    assert parse_epoch("1723668188\n") == 1723668188.0
    assert parse_epoch("") is None
    assert parse_epoch("not-a-number") is None


def test_read_facts_is_all_unknown_when_the_probe_fails():
    def boom(argv, timeout):
        raise OSError("powershell.exe not found")
    f = read_facts(run=boom)
    assert f == BootFacts(None, None, None, None, None)


def test_read_facts_composes_the_four_probe_lines_in_order(monkeypatch):
    # A known-good four-line probe result: machine boot, first login,
    # locked=1, autologin=0. Pins the field ordering `_parse_facts_text`
    # depends on -- the live host is the only other thing that exercises
    # this composition, and it isn't part of CI.
    def fake_run(argv, timeout):
        return "1723668188\n1723668209\n1\n0\n"

    monkeypatch.setattr(boot_windows, "_wsl_boot_epoch", lambda: 1723668227.5)
    f = read_facts(run=fake_run)
    assert f == BootFacts(
        machine_boot=1723668188.0,
        surface_boot=1723668227.5,
        first_login=1723668209.0,
        locked=True,
        autologin=False,
    )


def test_read_facts_reports_absent_lines_as_none(monkeypatch):
    # `-` marks an unreadable timestamp; a short/garbled probe result must
    # not be padded with a guess -- missing/short lines stay None.
    def fake_run(argv, timeout):
        return "-\n-\n0\n0\n"

    monkeypatch.setattr(boot_windows, "_wsl_boot_epoch", lambda: None)
    f = read_facts(run=fake_run)
    assert f == BootFacts(None, None, None, False, False)


def test_facts_command_is_read_only():
    # This module reads boot/login state; it must never be able to change
    # it. No Set-*, no reg add, no Register-* in the emitted script.
    argv = facts_command()
    script = " ".join(argv)
    assert "Set-" not in script
    assert "reg add" not in script
    assert "Register-" not in script
    assert "Get-" in script


# ---------------------------------------------------------------------------
# Linux: linger + surface-boot facts (spec 2026-08-14, Task 5)
# ---------------------------------------------------------------------------

from crr.adapters import boot_linux


def test_linger_enabled_reads_loginctl():
    def fake(argv, timeout):
        assert "show-user" in argv
        return "Linger=yes\n"
    assert boot_linux.linger_enabled("evan", run=fake) is True

    assert boot_linux.linger_enabled(
        "evan", run=lambda a, timeout: "Linger=no\n") is False


def test_linger_unknown_when_loginctl_fails():
    def boom(argv, timeout):
        raise OSError("no loginctl")
    assert boot_linux.linger_enabled("evan", run=boom) is None


def test_linger_unknown_on_garbled_output():
    # Neither "yes" nor "no" -- must not be guessed either way.
    assert boot_linux.linger_enabled(
        "evan", run=lambda a, timeout: "garbage\n") is None


def test_linux_read_facts_all_none_on_failure():
    def boom(argv, timeout):
        raise OSError("x")
    f = boot_linux.read_facts("evan", run=boom)
    assert f.machine_boot is None and f.surface_boot is None
    assert f == BootFacts(None, None, None, None, None)


def test_linux_read_facts_composes_all_three_timestamps():
    # A known-good triple: /proc/stat btime, crr-web.service
    # ActiveEnterTimestamp (converted via `date -d`, never guessed), and
    # two `last -F --time-format iso` login lines (earliest wins). The
    # ActiveEnterTimestamp line is deliberately a bare LOCAL string with a
    # tz abbreviation `date -d` alone can resolve -- this module must not
    # attempt its own UTC-assuming conversion of it (regression coverage
    # for the boot_windows-style local/UTC mixup).
    def fake(argv, timeout):
        if argv[:2] == ["cat", "/proc/stat"]:
            return "cpu  0 0 0 0 0 0 0 0 0 0\nbtime 1723668188\nprocesses 1\n"
        if argv[:3] == ["systemctl", "--user", "show"]:
            assert "crr-web.service" in argv
            assert "ActiveEnterTimestamp" in " ".join(argv)
            return "ActiveEnterTimestamp=Thu 2026-08-14 09:16:27 EDT\n"
        if argv[:4] == ["last", "-F", "--time-format", "iso"]:
            # `last` is scoped to the requested user, not left to filter
            # client-side.
            assert argv[-1] == "evan"
            return (
                "evan  pts/1  10.0.0.6  2026-08-14T09:20:10-04:00   still logged in\n"
                "evan  pts/0  10.0.0.5  2026-08-14T09:16:49-04:00   still logged in\n"
                "\n"
                "wtmp begins 2026-08-14T09:16:49-04:00\n"
            )
        if argv[:2] == ["date", "-d"]:
            # The RAW value from systemctl must be passed through
            # unmodified -- this fake proves that, then returns a
            # distinguishable epoch to prove the caller actually consumed
            # `date`'s answer rather than computing its own.
            assert argv[2] == "Thu 2026-08-14 09:16:27 EDT"
            return "1723641387\n"
        raise AssertionError(f"unexpected argv {argv}")

    f = boot_linux.read_facts("evan", run=fake)
    assert f.machine_boot == 1723668188.0
    assert f.surface_boot == 1723641387.0
    # The earlier of the two ISO login lines wins, not the first line seen.
    from datetime import datetime
    expected_first_login = datetime.fromisoformat(
        "2026-08-14T09:16:49-04:00"
    ).timestamp()
    assert f.first_login == expected_first_login
    # Not meaningful on Linux; the verdict does not require them.
    assert f.locked is None and f.autologin is None


def test_linux_read_facts_falls_back_to_stat_and_who():
    # `cat /proc/stat` and `last -F` both fail; the fallbacks (`stat
    # /proc/1`, `who`) still produce an answer. `who`'s bare local
    # timestamp is converted via `date -d` too, and a different user's
    # login line is excluded.
    def fake(argv, timeout):
        if argv[:2] == ["cat", "/proc/stat"]:
            raise OSError("no /proc/stat")
        if argv[:2] == ["stat", "-c"]:
            return "1723668188\n"
        if argv[:3] == ["systemctl", "--user", "show"]:
            return "ActiveEnterTimestamp=\n"  # never activated
        if argv[:4] == ["last", "-F", "--time-format"]:
            raise OSError("no last binary")
        if argv == ["who"]:
            return (
                "evan     pts/0        2026-08-14 09:16 (10.0.0.5)\n"
                "root     pts/1        2026-08-14 09:10 (10.0.0.9)\n"
            )
        if argv[:2] == ["date", "-d"]:
            assert argv[2] == "2026-08-14 09:16"   # evan's line, not root's
            return "1723668960\n"
        raise AssertionError(f"unexpected argv {argv}")

    f = boot_linux.read_facts("evan", run=fake)
    assert f.machine_boot == 1723668188.0
    assert f.surface_boot is None    # "never activated" is an honest unknown
    assert f.first_login == 1723668960.0
    assert f.locked is None and f.autologin is None


def test_linux_read_facts_excludes_logins_before_machine_boot():
    # `last` walks wtmp across EVERY previous boot; a login from a prior
    # boot must not be reported as "first login this boot". machine_boot
    # is computed to fall strictly between the two login lines below, so
    # this pins the floor filter rather than an accidental ordering.
    from datetime import datetime
    machine_boot_epoch = datetime.fromisoformat("2026-08-14T09:16:00-04:00").timestamp()

    def fake(argv, timeout):
        if argv[:2] == ["cat", "/proc/stat"]:
            return f"btime {int(machine_boot_epoch)}\n"
        if argv[:3] == ["systemctl", "--user", "show"]:
            return "ActiveEnterTimestamp=\n"
        if argv[:4] == ["last", "-F", "--time-format", "iso"]:
            return (
                # Stale: before machine_boot -- a previous boot's login,
                # must be filtered out.
                "evan  pts/0  -  2026-08-14T05:00:00-04:00   still logged in\n"
                # Genuine: after machine_boot.
                "evan  pts/1  -  2026-08-14T09:20:10-04:00   still logged in\n"
            )
        raise AssertionError(f"unexpected argv {argv}")

    f = boot_linux.read_facts("evan", run=fake)
    expected = datetime.fromisoformat("2026-08-14T09:20:10-04:00").timestamp()
    assert f.first_login == expected


# ---------------------------------------------------------------------------
# macOS: LaunchDaemon boot daemon + FileVault detection (spec 2026-08-14, Task 6)
# ---------------------------------------------------------------------------

import plistlib

from crr.adapters import boot_macos


def test_web_daemon_is_a_boot_daemon_not_a_login_agent():
    parsed = plistlib.loads(
        boot_macos.web_daemon_plist("/opt/crr/bin/crr", "/usr/bin", 8765).encode())
    assert parsed["Label"] == boot_macos.DAEMON_LABEL
    # RunAtLoad + KeepAlive so it starts at boot, before login — the whole
    # point vs. crr's existing LaunchAgents which need a GUI session.
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["ProgramArguments"][:2] == ["/opt/crr/bin/crr", "web"]


def test_filevault_parsing():
    assert boot_macos.filevault_enabled(
        run=lambda a, timeout: "FileVault is On.\n") is True
    assert boot_macos.filevault_enabled(
        run=lambda a, timeout: "FileVault is Off.\n") is False
    assert boot_macos.filevault_enabled(
        run=lambda a, timeout: "???\n") is None


# ---------------------------------------------------------------------------
# CLI wiring: `crr reachable-at-boot` -- report, --install, --uninstall
# (spec 2026-08-14, Task 7)
# ---------------------------------------------------------------------------

from crr import cli
from crr.adapters.boot_windows import BootFacts


def _cfg():
    return {"boot_headless_window_seconds": 300, "boot_preferred_tailnet": "",
            "interop_timeout_seconds": 5, "dashboard_port": 8765}


def test_report_says_headless_when_the_facts_show_it(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli.boot_windows, "read_facts",
                        lambda **k: BootFacts(0.0, 39.0, None, True, False))
    assert cli.main(["reachable-at-boot"]) == 0
    out = capsys.readouterr().out.lower()
    assert "headless" in out and "survivable" in out


def test_report_never_claims_headless_on_unknown(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli.boot_windows, "read_facts",
                        lambda **k: BootFacts(None, None, None, None, None))
    cli.main(["reachable-at-boot"])
    out = capsys.readouterr().out.lower()
    assert "headless" not in out
    assert "unknown" in out or "could not" in out


def test_install_refuses_without_a_tty(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["reachable-at-boot", "--install"]) != 0
    assert ran == []


def test_install_runs_the_generated_script_once_confirmed(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli, "_wsl_distro_and_user", lambda: ("Ubuntu-24.04", "evan"))
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["reachable-at-boot", "--install"]) == 0
    assert ran, "confirmed but ran nothing"
    # the elevated register goes through powershell RunAs (mirrors harden)
    assert any("RunAs" in " ".join(c) for c in ran)


def test_macos_install_refuses_under_filevault(monkeypatch, capsys):
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_load_config", _cfg)
    monkeypatch.setattr(cli.boot_macos, "filevault_enabled", lambda **k: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = cli.main(["reachable-at-boot", "--install"])
    assert rc != 0
    assert "filevault" in capsys.readouterr().err.lower()
