"""Boot-identity adapters (implement crr.core.ports.BootIdentity).

Linux reads ``/proc/sys/kernel/random/boot_id``; macOS parses
``sysctl -n kern.boottime`` (Phase 2). Detection + selection is the
composition root's job (crr.cli), not this module's — an adapter never
reaches up to decide which adapter runs.
"""

from __future__ import annotations

import platform
import re
import subprocess

from crr.core.ports import BootIdentity  # adapters may import core (down-arrow)

_LINUX_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_BOOTTIME_SEC_RE = re.compile(r"sec\s*=\s*(\d+)")


class LinuxBootIdentity:
    """BootIdentity backed by the kernel's random boot_id (Linux only)."""

    def current(self) -> str:
        with open(_LINUX_BOOT_ID_PATH, "r", encoding="ascii") as fh:
            return fh.read().strip()


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
