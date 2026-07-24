"""Boot-identity adapters (implement crr.core.ports.BootIdentity).

Linux reads ``/proc/sys/kernel/random/boot_id``; macOS parses
``sysctl -n kern.boottime`` (Phase 2). Detection + selection is the
composition root's job (crr.cli), not this module's — an adapter never
reaches up to decide which adapter runs.
"""

from __future__ import annotations

import platform

from crr.core.ports import BootIdentity  # adapters may import core (down-arrow)

_LINUX_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


class LinuxBootIdentity:
    """BootIdentity backed by the kernel's random boot_id (Linux only)."""

    def current(self) -> str:
        with open(_LINUX_BOOT_ID_PATH, "r", encoding="ascii") as fh:
            return fh.read().strip()


def detect() -> BootIdentity:
    """Return the boot-identity adapter for the current platform.

    Phase 0 only ships Linux; other platforms raise so the failure is
    loud rather than a silently-wrong identity (which would misclassify
    crashed sessions as live).
    """
    system = platform.system()
    if system == "Linux":
        return LinuxBootIdentity()
    raise NotImplementedError(f"no boot-identity adapter for {system!r} yet")
