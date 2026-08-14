"""macOS boot daemon plist + FileVault detection (unverified).

This module is UNVERIFIED — no Mac hardware available (#43). Generation is
tested; runtime behavior is stated as untested.

FileVault blocks all boot execution until the disk is unlocked at the
pre-boot screen. If ``filevault_enabled()`` returns True, the daemon cannot
survive a reboot headless. A consumer MUST refuse ``--install`` when
FileVault is enabled.

The macOS analogue of the SystemD linger and Windows scheduled task. A
LaunchDaemon (root, ``/Library/LaunchDaemons``) runs ``crr web`` at system
boot, before login, so the dashboard survives a reboot headless (if
FileVault is disabled and the machine has network access).

Plists are serialized with ``plistlib`` (stdlib — no runtime dep, correct
Apple XML + DOCTYPE + escaping) rather than hand-written strings.

LaunchDaemons require PATH baking — see ``crr/adapters/launchd.py`` for
rationale.

Invoked only from crr.cli; no core port (core never spawns services).
"""

from __future__ import annotations

import plistlib
import re
from typing import Callable

DAEMON_LABEL = "com.claude-remote-rescue.web-daemon"


def web_daemon_plist(crr_bin: str, path: str, port: int) -> str:
    """The dashboard daemon (boot; root; headless).

    RunAtLoad + KeepAlive start the daemon at boot and keep it running,
    so the dashboard survives a reboot headless (on machines with network
    access and FileVault disabled).
    """
    return plistlib.dumps({
        "Label": DAEMON_LABEL,
        "ProgramArguments": [crr_bin, "web", "--port", str(port)],
        "KeepAlive": True,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": path},
    }).decode("utf-8")


def filevault_enabled(run: Callable[[list[str], int], str] | None = None) -> bool | None:
    """Detect whether FileVault is enabled.

    Parses ``fdesetup status``:
    - "FileVault is On." → True
    - "FileVault is Off." → False
    - Anything else → None (unknown or error)

    Args:
        run: Optional test injection for running commands. Signature:
            run(argv: list[str], timeout: int) -> str

    Returns:
        True if FileVault is enabled, False if disabled, None if unknown or error.
    """
    if run is None:
        import subprocess
        run = subprocess.run

    try:
        if callable(run) and hasattr(run, "__self__"):
            # It's a bound method (e.g., subprocess.run)
            output = run(["fdesetup", "status"], capture_output=True, text=True, timeout=5).stdout
        else:
            # It's a plain function (e.g., test injected callable)
            output = run(["fdesetup", "status"], timeout=5)
    except Exception:
        return None

    if "FileVault is On." in output:
        return True
    elif "FileVault is Off." in output:
        return False
    else:
        return None
