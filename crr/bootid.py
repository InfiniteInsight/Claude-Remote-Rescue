"""Boot-identity platform adapter.

A boot identity is an opaque string that changes on every host boot. It is
the guard against the recycled-pid hazard: a journal entry whose boot_id
does not match the current boot refers to a process from a previous life
of the machine, no matter what pid-existence checks say.

- Linux: /proc/sys/kernel/random/boot_id
- macOS: `sysctl -n kern.boottime` (the boot timestamp is unique per boot)
"""

from __future__ import annotations

import subprocess
import sys

_LINUX_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def current_boot_id() -> str:
    """Return the current boot identity, or "" if it cannot be determined.

    Callers must treat "" as "unknown" and never as a match.
    """
    if sys.platform == "darwin":
        return _darwin_boot_id()
    return _linux_boot_id()


def _linux_boot_id() -> str:
    try:
        with open(_LINUX_BOOT_ID_PATH, "r", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _darwin_boot_id() -> str:
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def same_boot(boot_id: str) -> bool:
    """True when *boot_id* matches the current boot identity.

    An empty or unknown identity on either side never matches: we would
    rather classify a session as crashed than risk signalling a recycled
    pid.
    """
    current = current_boot_id()
    return bool(boot_id) and bool(current) and boot_id == current
