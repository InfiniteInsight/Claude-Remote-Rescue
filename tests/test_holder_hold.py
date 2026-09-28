"""Holder hold flag (#138 follow-up) — pure core.

The WSL holder task re-arms every minute, so an intentional `wsl --shutdown`
needs a way to keep it down. The hold flag lives on the Windows side (the
task must decide BEFORE it starts WSL); the launcher script and `crr holder
status` apply the same rules, which this module states once.
"""

import pytest

from crr.core import contracts, holder_hold as hh

_NOW = 1_790_600_000
_BOOT = 1_789_800_000


def test_no_flag_means_the_holder_runs():
    assert hh.holds(None, now=_NOW, machine_boot=_BOOT) is False
    assert hh.describe(None, now=_NOW, machine_boot=_BOOT) == "active"


def test_pause_holds_until_it_expires():
    rec = hh.pause(now=_NOW, minutes=30)
    contracts.validate_holder_hold(rec)
    assert hh.holds(rec, now=_NOW + 29 * 60, machine_boot=_BOOT)
    assert not hh.holds(rec, now=_NOW + 30 * 60, machine_boot=_BOOT)
    assert "paused" in hh.describe(rec, now=_NOW, machine_boot=_BOOT)
    assert "expired" in hh.describe(rec, now=_NOW + 31 * 60, machine_boot=_BOOT)


def test_pause_rejects_a_non_positive_duration():
    with pytest.raises(ValueError):
        hh.pause(now=_NOW, minutes=0)


def test_stop_holds_until_resumed():
    rec = hh.stop(now=_NOW)
    contracts.validate_holder_hold(rec)
    assert hh.holds(rec, now=_NOW + 10**7, machine_boot=_BOOT + 10**6)
    assert hh.describe(rec, now=_NOW, machine_boot=_BOOT).startswith("stopped")


def test_stop_until_reboot_releases_on_a_new_windows_boot():
    rec = hh.stop_until_reboot(now=_NOW, machine_boot=_BOOT)
    contracts.validate_holder_hold(rec)
    assert hh.holds(rec, now=_NOW, machine_boot=_BOOT)
    assert hh.holds(rec, now=_NOW, machine_boot=_BOOT + 2)  # read jitter
    assert not hh.holds(rec, now=_NOW, machine_boot=_BOOT + 3600)
    assert "reboot" in hh.describe(rec, now=_NOW, machine_boot=_BOOT)


def test_stop_until_reboot_is_honest_when_the_boot_time_is_unknown():
    rec = hh.stop_until_reboot(now=_NOW, machine_boot=_BOOT)
    # Can't tell whether Windows rebooted: keep holding (the safe reading of
    # an explicit stop) and say it's unknown rather than guessing.
    assert hh.holds(rec, now=_NOW, machine_boot=None)
    assert "unknown" in hh.describe(rec, now=_NOW, machine_boot=None)


def test_an_unrecognized_flag_never_holds():
    # Fail open: a flag from a future build must not strand the machine
    # unreachable.
    assert hh.holds({"v": 99, "mode": "stop"}, now=_NOW, machine_boot=_BOOT) is False
    assert hh.holds({"v": 1, "mode": "weird"}, now=_NOW, machine_boot=_BOOT) is False


def test_validator_rejects_malformed_records():
    for bad in ({}, {"v": 1, "mode": "pause", "set_at": _NOW},
                {"v": 1, "mode": "until-reboot", "set_at": _NOW},
                {"v": 1, "mode": "stop", "set_at": "x"}):
        with pytest.raises(contracts.ContractError):
            contracts.validate_holder_hold(bad)
