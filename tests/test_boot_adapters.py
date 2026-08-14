"""reachable-at-boot adapters — generation asserted as text; NO registration.

Nothing here registers a Scheduled Task, writes the registry, or reboots. The
elevated register path is exercised with an injected runner in the CLI tests.
"""

from crr.adapters.boot_windows import (TAILNET_TASK, WSL_BOOT_TASK,
                                       install_script, tailnet_script,
                                       wsl_boot_argument)


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
