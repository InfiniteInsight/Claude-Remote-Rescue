"""Boot-identity adapters (implement crr.core.ports.BootIdentity).

Linux reads ``/proc/sys/kernel/random/boot_id`` plus PID 1's start time
(``<boot_id>@<ticks>``, #138 — on WSL a distro restart keeps the VM kernel
and its boot_id, while PID 1 restarts and pids recycle); macOS parses
``sysctl -n kern.boottime`` (Phase 2). Detection + selection is the
composition root's job (crr.cli), not this module's — an adapter never
reaches up to decide which adapter runs.
"""

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

from crr.core.classifier import BOOT_ID_SEPARATOR
from crr.core.ports import BootIdentity  # adapters may import core (down-arrow)

_LINUX_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_LINUX_INIT_STAT_PATH = "/proc/1/stat"
# /proc/<pid>/stat field 22 (starttime); fields after the ")" that closes
# comm start at field 3, so starttime is index 19 of that remainder.
_STARTTIME_AFTER_COMM = 19
_BOOTTIME_SEC_RE = re.compile(r"sec\s*=\s*(\d+)")


def _parse_init_start(stat_text: str) -> str:
    """PID 1's start time (clock ticks since kernel boot) from its stat line.

    comm (field 2) may itself contain spaces and parens, so split after the
    LAST ")" rather than on whitespace from the start.
    """
    _, sep, rest = stat_text.rpartition(")")
    fields = rest.split()
    if not sep or len(fields) <= _STARTTIME_AFTER_COMM \
            or not fields[_STARTTIME_AFTER_COMM].isdigit():
        raise ValueError(f"could not parse starttime from stat: {stat_text[:80]!r}")
    return fields[_STARTTIME_AFTER_COMM]


class LinuxBootIdentity:
    """BootIdentity for one PID-1 lifetime: kernel boot_id + init start.

    An unreadable PID-1 stat degrades to the bare kernel id — exactly the
    pre-#138 identity, which ``classifier.same_boot`` still matches against
    compound ids — rather than failing every crr command.
    """

    def __init__(self, boot_id_path=_LINUX_BOOT_ID_PATH,
                 init_stat_path=_LINUX_INIT_STAT_PATH) -> None:
        self._boot_id_path = Path(boot_id_path)
        self._init_stat_path = Path(init_stat_path)

    def current(self) -> str:
        kernel = self._boot_id_path.read_text(encoding="ascii").strip()
        try:
            start = _parse_init_start(self._init_stat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return kernel
        return f"{kernel}{BOOT_ID_SEPARATOR}{start}"


def _parse_boottime(sysctl_output: str) -> str:
    """Extract the boot-time seconds from ``sysctl -n kern.boottime`` output.

    The value looks like ``{ sec = 1784723478, usec = 0 } <date>``. The
    seconds field is a stable per-boot identity: it changes on reboot, so a
    journaled entry from a prior boot mismatches and classifies crashed.
    """
    match = _BOOTTIME_SEC_RE.search(sysctl_output)
    if not match:
        raise ValueError(f"could not parse kern.boottime: {sysctl_output!r}")
    return match.group(1)


class MacBootIdentity:
    """BootIdentity from ``sysctl -n kern.boottime`` (macOS only)."""

    def current(self) -> str:
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True, text=True, check=True,
        ).stdout
        return _parse_boottime(out)


def detect() -> BootIdentity:
    """Return the boot-identity adapter for the current platform.

    Unsupported platforms raise so the failure is loud rather than a
    silently-wrong identity (which would misclassify crashed sessions as
    live).
    """
    system = platform.system()
    if system == "Linux":
        return LinuxBootIdentity()
    if system == "Darwin":
        return MacBootIdentity()
    raise NotImplementedError(f"no boot-identity adapter for {system!r} yet")
