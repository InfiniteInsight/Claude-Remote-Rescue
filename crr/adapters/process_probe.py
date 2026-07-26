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
from typing import Sequence

# tty strings that mean "no controlling terminal".
_NO_TTY = {"?", "??"}


def _tty_is_real(raw: str) -> bool:
    """True if a `ps -o tty=` value denotes a real controlling terminal."""
    value = raw.strip()
    return bool(value) and value not in _NO_TTY


def _parse_tty_pids(stdout: str) -> set[int]:
    """Parse ``ps -o tty=,pid=`` output into the set of pids with a real tty.

    Each line is ``<tty> <pid>`` (tty first, pid last); a pid is included
    only when its tty column denotes a real controlling terminal.
    """
    out: set[int] = set()
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if _tty_is_real(parts[0]):
            try:
                out.add(int(parts[-1]))
            except ValueError:
                continue
    return out


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

    def controlling_ttys(self, pids: Sequence[int]) -> set[int]:
        ids = [int(p) for p in pids]
        if not ids:
            return set()  # never `ps` with no -p (it would list every process)
        try:
            result = subprocess.run(
                ["ps", "-o", "tty=,pid=", "-p", ",".join(str(p) for p in ids)],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return set()
        if result.returncode != 0:
            return set()
        return _parse_tty_pids(result.stdout)
