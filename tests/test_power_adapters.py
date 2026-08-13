"""Power adapters (spec 2026-08-12).

The AC probe is measured, not assumed: WSL2 passes the host battery
through sysfs (`/sys/class/power_supply/AC1/online`), which is why ONE
Linux adapter serves both native Linux and WSL.
"""

import subprocess as _sp
import sys as _sys
from pathlib import Path

import pytest

from crr.adapters.power_source import (MacPowerSource, SysfsPowerSource,
                                       _parse_pmset)
from crr.adapters.power_hold_linux import (LinuxPowerHolder, inhibit_argv,
                                           lid_is_exempt)
from crr.adapters.power_hold_macos import MacPowerHolder, caffeinate_argv
from crr.adapters.power_hold_windows import (WindowsPowerHolder,
                                             holder_argv, holder_script)


def _supply(root: Path, name: str, **files: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for key, value in files.items():
        (d / key).write_text(value + "\n", encoding="utf-8")


def test_mains_online_reads_as_on_ac(tmp_path):
    _supply(tmp_path, "AC1", type="Mains", online="1")
    _supply(tmp_path, "BAT1", type="Battery", status="Full")
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_mains_offline_reads_as_on_battery(tmp_path):
    _supply(tmp_path, "AC1", type="Mains", online="0")
    _supply(tmp_path, "BAT1", type="Battery", status="Discharging")
    assert SysfsPowerSource(tmp_path).on_ac() is False


def test_a_machine_with_no_power_supplies_is_a_desktop_not_an_unknown(tmp_path):
    # Known True, not None: a desktop is always on mains. Returning None
    # here would withhold the hold on every server and every VM.
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_a_battery_with_no_mains_device_falls_back_to_its_status(tmp_path):
    _supply(tmp_path, "BAT0", type="Battery", status="Discharging")
    assert SysfsPowerSource(tmp_path).on_ac() is False
    _supply(tmp_path, "BAT0", type="Battery", status="Charging")
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_an_unreadable_probe_is_unknown_not_a_guess(tmp_path):
    _supply(tmp_path, "AC1", type="Mains")   # no `online` file at all
    assert SysfsPowerSource(tmp_path).on_ac() is None


def test_a_missing_root_is_unknown(tmp_path):
    assert SysfsPowerSource(tmp_path / "nope").on_ac() is None


def test_a_device_whose_type_is_unreadable_is_unknown_not_mains(tmp_path):
    import os
    # Skip if running as root (root ignores chmod 000)
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    # Create a device directory with an unreadable type file
    d = tmp_path / "AC1"
    d.mkdir(parents=True, exist_ok=True)
    type_file = d / "type"
    type_file.write_text("Mains\n", encoding="utf-8")
    type_file.chmod(0o000)
    # Should return None (unknown), not True (guessing mains)
    assert SysfsPowerSource(tmp_path).on_ac() is None


@pytest.mark.parametrize("text,expected", [
    ("Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged", True),
    ("Now drawing from 'Battery Power'\n -InternalBattery-0 82%", False),
    ("something unparseable", None),
    ("", None),
])
def test_pmset_parsing(text, expected):
    assert _parse_pmset(text) is expected


def test_inhibit_asks_for_sleep_not_idle():
    # `idle` inhibits logind's IdleAction, which defaults to `ignore` — it
    # would hold successfully and protect NOTHING. `sleep` is what GNOME
    # and KDE's idle-suspend actually goes through. This is the opposite
    # of the obvious choice; see the spec before "fixing" it.
    argv = inhibit_argv(frozenset({"sleep"}), "crr: 2 Claude sessions live")
    what = argv[argv.index("--what") + 1] if "--what" in argv else ""
    joined = " ".join(argv)
    assert "sleep" in what, f"must inhibit sleep, got {joined}"
    assert "idle" not in what, (
        "idle inhibits IdleAction, which defaults to ignore — a hold that "
        f"protects nothing. Got {joined}")


def test_inhibit_adds_shutdown_only_when_asked():
    both = inhibit_argv(frozenset({"sleep", "shutdown"}), "r")
    what = both[both.index("--what") + 1]
    assert set(what.split(":")) == {"sleep", "shutdown"}


def test_inhibit_is_block_mode_and_carries_the_reason():
    argv = inhibit_argv(frozenset({"sleep"}), "crr: 1 Claude session live")
    assert argv[0] == "systemd-inhibit"
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "block"
    assert "crr: 1 Claude session live" in argv


@pytest.mark.parametrize("conf,exempt", [
    ("", True),                                    # unset -> default yes
    ("#LidSwitchIgnoreInhibited=yes\n", True),     # commented -> default
    ("LidSwitchIgnoreInhibited=yes\n", True),
    ("LidSwitchIgnoreInhibited=no\n", False),
    ("[Login]\nLidSwitchIgnoreInhibited = no\n", False),
])
def test_lid_exemption_is_read_not_assumed(conf, exempt):
    assert lid_is_exempt(conf) is exempt


def test_holder_refuses_to_block_sleep_when_the_lid_is_not_exempt(tmp_path):
    # The builder's hard requirement is that closing the lid always
    # sleeps. On a host that has turned the default off, a sleep lock
    # would break that, so crr withholds instead.
    conf = tmp_path / "logind.conf"
    conf.write_text("LidSwitchIgnoreInhibited=no\n", encoding="utf-8")
    spawned = []
    holder = LinuxPowerHolder(logind_conf=conf,
                              spawn=lambda argv, **kw: spawned.append(argv))
    holder.hold(frozenset({"sleep"}), "r")
    assert spawned == [], "blocked sleep on a host where that blocks the lid"
    assert holder.held() == frozenset()


def test_holder_still_blocks_shutdown_when_the_lid_is_not_exempt(tmp_path):
    # Only the sleep half is unsafe there; shutdown is unaffected by lid
    # handling, so withholding it too would be over-correction.
    conf = tmp_path / "logind.conf"
    conf.write_text("LidSwitchIgnoreInhibited=no\n", encoding="utf-8")
    spawned = []

    class _P:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    def _spawn(argv, **kw):
        spawned.append(argv)
        return _P()

    holder = LinuxPowerHolder(logind_conf=conf, spawn=_spawn)
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"shutdown"})
    what = spawned[0][spawned[0].index("--what") + 1]
    assert what == "shutdown"


def test_capabilities_are_both_on_linux(tmp_path):
    holder = LinuxPowerHolder(logind_conf=tmp_path / "absent.conf")
    assert holder.capabilities() == frozenset({"sleep", "shutdown"})


def test_macos_can_hold_sleep_but_not_shutdown():
    # Not an omission. A launch daemon cannot block a macOS shutdown at
    # all: the cancellable notifications do not reach daemons, and only a
    # GUI app in the login session can delay one. Deferred by the spec.
    assert MacPowerHolder().capabilities() == frozenset({"sleep"})


def test_caffeinate_holds_idle_only_so_the_lid_still_sleeps():
    argv = caffeinate_argv()
    assert argv[0] == "caffeinate"
    assert "-i" in argv
    assert "-s" not in argv, "-s would fight the lid; idle only"


def test_macos_holder_ignores_a_shutdown_request_it_cannot_serve():
    spawned = []

    class _P:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    holder = MacPowerHolder(spawn=lambda argv, **kw: spawned.append(argv) or _P())
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"sleep"})
    assert len(spawned) == 1


def test_windows_claims_both_capabilities():
    assert WindowsPowerHolder().capabilities() == frozenset(
        {"sleep", "shutdown"})


def test_script_sets_execution_state_for_sleep():
    s = holder_script(frozenset({"sleep"}), "crr: 1 Claude session live")
    assert "SetThreadExecutionState" in s
    assert "0x80000001" in s or ("ES_CONTINUOUS" in s and "ES_SYSTEM_REQUIRED" in s)
    assert "ShutdownBlockReasonCreate" not in s


def test_script_registers_a_block_reason_for_shutdown():
    s = holder_script(frozenset({"sleep", "shutdown"}), "crr: 2 live")
    assert "ShutdownBlockReasonCreate" in s
    assert "crr: 2 live" in s


def test_script_exits_when_stdin_closes():
    # THE orphan defence. Without this a killed crr leaves a PowerShell
    # holding a shutdown block forever, and the user has a machine that
    # refuses to restart with nothing left running to explain why.
    s = holder_script(frozenset({"sleep"}), "r")
    assert "ReadLine" in s, "no stdin-EOF loop: an orphan would hold forever"


def test_script_self_releases_after_the_cap():
    s = holder_script(frozenset({"sleep"}), "r", max_hours=12)
    assert "12" in s


def test_argv_runs_powershell_noninteractively_with_stdin_open():
    argv = holder_argv()
    assert argv[0] == "powershell.exe"
    assert "-NoProfile" in argv
    assert "-Command" in argv
    assert "-NonInteractive" not in argv, (
        "the holder READS stdin as its liveness signal; -NonInteractive "
        "would defeat the orphan defence")


def test_a_stdin_eof_child_exits_promptly():
    # Platform-independent proof of the MECHANISM the Windows script uses:
    # a child reading stdin to EOF must exit when the pipe closes. The
    # PowerShell equivalent is asserted by inspection above; this asserts
    # the pattern actually terminates a process.
    proc = _sp.Popen([_sys.executable, "-c",
                      "import sys; sys.stdin.read()"], stdin=_sp.PIPE)
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0
