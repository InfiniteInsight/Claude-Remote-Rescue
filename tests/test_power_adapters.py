"""Power adapters (spec 2026-08-12).

The AC probe is measured, not assumed: WSL2 passes the host battery
through sysfs (`/sys/class/power_supply/AC1/online`), which is why ONE
Linux adapter serves both native Linux and WSL.
"""

from pathlib import Path

import pytest

from crr.adapters.power_source import (MacPowerSource, SysfsPowerSource,
                                       _parse_pmset)


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


@pytest.mark.parametrize("text,expected", [
    ("Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged", True),
    ("Now drawing from 'Battery Power'\n -InternalBattery-0 82%", False),
    ("something unparseable", None),
    ("", None),
])
def test_pmset_parsing(text, expected):
    assert _parse_pmset(text) is expected
