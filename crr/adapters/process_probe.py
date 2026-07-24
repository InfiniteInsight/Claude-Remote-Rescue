"""Process-probe adapter (implements crr.core.ports.ProcessProbe).

``is_alive`` uses ``os.kill(pid, 0)`` — portable across Linux/macOS, no
subprocess. ``has_controlling_tty`` shells out to ``ps -o tty= -p <pid>``
per DESIGN.md (portable, avoids /proc so it also works on macOS), guarded
by an interop timeout that the composition root sources from config
(never a magic number here).

If the tty check cannot be determined (timeout, ps missing, error), it
returns False: we degrade toward ``ghost``/``crashed`` rather than
claiming a session is ``live`` on unknown evidence.
"""

from __future__ import annotations

import os
import subprocess

# tty strings that mean "no controlling terminal".
_NO_TTY = {"?", "??"}


def _tty_is_real(raw: str) -> bool:
    """True if a `ps -o tty=` value denotes a real controlling terminal."""
    value = raw.strip()
    return bool(value) and value not in _NO_TTY


class PsProcessProbe:
    def __init__(self, timeout_seconds: float) -> None:
        # Sourced from config (interop_timeout_seconds) by the caller.
        self._timeout = timeout_seconds

    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but we may not signal it — still alive
        return True

    def has_controlling_tty(self, pid: int) -> bool:
        try:
            result = subprocess.run(
                ["ps", "-o", "tty=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        if result.returncode != 0:
            return False
        return _tty_is_real(result.stdout)
