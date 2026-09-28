"""Hold flag for the WSL holder task (#138 follow-up).

``crr-wsl-boot`` re-arms every minute so a distro restart can't strand
sessions — which also means a deliberate ``wsl --shutdown`` comes straight
back. A hold flag keeps it down: paused for N minutes, stopped until
resumed, or stopped until the next Windows reboot.

The flag lives on the Windows side (the task must decide BEFORE starting
WSL). Two readers apply these rules: ``crr holder status`` via this module,
and the PowerShell launcher ``boot_windows.holder_script`` generates. Both
fail OPEN — an unreadable or unknown flag lets the holder run, because a
stranded, unreachable machine is the worse failure.

Pure: epochs in, records/decisions out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from crr.core import contracts

# Windows' LastBootUpTime read twice can differ by rounding; a real reboot
# moves it by far more than this.
BOOT_MATCH_TOLERANCE_SECONDS = 5


def _record(mode: str, now: int, **extra: int) -> dict[str, Any]:
    rec = {"v": contracts.HOLDER_HOLD_STORE_VERSION, "mode": mode,
           "set_at": int(now), **{k: int(v) for k, v in extra.items()}}
    contracts.validate_holder_hold(rec)
    return rec


def pause(*, now: int, minutes: int) -> dict[str, Any]:
    if minutes <= 0:
        raise ValueError("pause needs a positive number of minutes")
    return _record("pause", now, until=int(now) + int(minutes) * 60)


def stop(*, now: int) -> dict[str, Any]:
    return _record("stop", now)


def stop_until_reboot(*, now: int, machine_boot: int) -> dict[str, Any]:
    return _record("until-reboot", now, boot=machine_boot)


def _valid(record: Mapping[str, Any] | None) -> bool:
    if record is None:
        return False
    try:
        contracts.validate_holder_hold(record)
    except contracts.ContractError:
        return False
    return True


def _same_windows_boot(recorded: int, machine_boot: float | None) -> bool | None:
    if machine_boot is None:
        return None
    return abs(recorded - machine_boot) <= BOOT_MATCH_TOLERANCE_SECONDS


def holds(record: Mapping[str, Any] | None, *, now: float,
          machine_boot: float | None) -> bool:
    """Should the holder stay down right now?"""
    if not _valid(record):
        return False
    mode = record["mode"]
    if mode == "stop":
        return True
    if mode == "pause":
        return now < record["until"]
    same = _same_windows_boot(record["boot"], machine_boot)
    return same is not False  # unknown boot: honour the explicit stop


def _clock(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().strftime("%H:%M")


def describe(record: Mapping[str, Any] | None, *, now: float,
             machine_boot: float | None) -> str:
    """One human line for ``crr holder status``."""
    if record is None:
        return "active"
    if not _valid(record):
        return "active (unrecognized hold flag ignored)"
    mode = record["mode"]
    if mode == "stop":
        return "stopped until `crr holder resume` (survives Windows reboots)"
    if mode == "pause":
        if now < record["until"]:
            return f"paused until {_clock(record['until'])}"
        return f"active (pause expired at {_clock(record['until'])})"
    same = _same_windows_boot(record["boot"], machine_boot)
    if same is None:
        return "stopped until the next Windows reboot (current boot time unknown)"
    if same:
        return "stopped until the next Windows reboot"
    return "active (stop-until-reboot expired: Windows has rebooted)"
