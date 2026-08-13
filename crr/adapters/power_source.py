"""Is this machine on mains power? (implements crr.core.ports.PowerSource)

One Linux adapter covers native Linux AND WSL: measured on 2026-08-12,
WSL2 passes the Windows host's battery through sysfs
(`/sys/class/power_supply/AC1/online` read 1 while Windows reported
`BatteryStatus=2`). So the WSL case needs no interop here — unlike the
HOLD, which does.

Every failure path returns None rather than a guess. "I could not read the
power source" is not "on battery", and it is not "on AC".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SYSFS_ROOT = Path("/sys/class/power_supply")

# `upower`-free and `pmset`-free on Linux by design: reading two files
# beats shelling out on a path that runs every poll.
_CHARGING = ("charging", "full", "not charging")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


class SysfsPowerSource:
    """PowerSource from ``/sys/class/power_supply`` (Linux and WSL)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = SYSFS_ROOT if root is None else Path(root)

    def on_ac(self) -> bool | None:
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            return None
        mains: list[str] = []
        batteries: list[str] = []
        for entry in entries:
            kind = _read(entry / "type")
            if kind == "Mains":
                value = _read(entry / "online")
                if value is not None:
                    mains.append(value)
            elif kind == "Battery":
                status = _read(entry / "status")
                if status is not None:
                    batteries.append(status.lower())
        if mains:
            return any(v == "1" for v in mains)
        if batteries:
            # No mains device exposed (some laptops, some VMs): the
            # battery's own status still answers the question.
            return any(s in _CHARGING for s in batteries)
        # No power-supply devices at all. That is a desktop or a server —
        # a KNOWN mains machine, not an unknown one. Returning None here
        # would withhold the hold on every non-laptop.
        if not any(_read(e / "type") for e in entries):
            return True
        return None


def _parse_pmset(text: str) -> bool | None:
    """True/False from ``pmset -g batt``; None when it does not say."""
    lowered = text.lower()
    if "'ac power'" in lowered:
        return True
    if "'battery power'" in lowered:
        return False
    return None


class MacPowerSource:
    """PowerSource from ``pmset -g batt`` (macOS)."""

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def on_ac(self) -> bool | None:
        try:
            result = subprocess.run(
                ["pmset", "-g", "batt"],
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        return _parse_pmset(result.stdout)
