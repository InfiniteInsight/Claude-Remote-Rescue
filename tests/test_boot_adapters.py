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
