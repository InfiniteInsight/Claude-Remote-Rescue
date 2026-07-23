"""Session classifier: live / ghost / crashed.

Computed at read time from a journal entry (DESIGN.md):

- live    -- same boot identity AND pid alive AND the shell owns a
             controlling terminal.
- ghost   -- same boot, pid alive, no controlling terminal.
             [lesson: window-close orphans] Closing a terminal window can
             kill the child process group but orphan the shell; without
             this state the dashboard shows healthy sessions that don't
             exist.
- crashed -- pid dead OR boot identity mismatch.

[lesson: recycled pids] Every destructive operation gates on this
classifier -- never on bare pid-existence -- or a reboot-recycled pid gets
an unrelated process killed. The boot check therefore comes first and an
unknown boot identity is treated as a mismatch.

The controlling-terminal check is `ps -o tty= -p <pid>`: portable (no
/proc), works on macOS.
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, Optional

from . import bootid

LIVE = "live"
GHOST = "ghost"
CRASHED = "crashed"


def pid_alive(pid: int) -> bool:
    """True when a process with *pid* exists.

    EPERM means the process exists but belongs to someone else: alive.
    """
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _ps_tty(pid: int) -> str:
    """Raw `ps -o tty=` output for *pid* ("" on any failure)."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def has_controlling_tty(pid: int) -> bool:
    tty = _ps_tty(pid)
    # ps prints "?" (Linux) or "??" (macOS) for no controlling terminal.
    return bool(tty) and tty not in ("?", "??", "-")


def classify(entry: Dict, current_boot_id: Optional[str] = None) -> str:
    """Classify a journal entry as live / ghost / crashed."""
    if current_boot_id is None:
        current_boot_id = bootid.current_boot_id()
    entry_boot = entry.get("boot_id") or ""
    if not entry_boot or not current_boot_id or entry_boot != current_boot_id:
        # Boot mismatch (or unknown): whatever lives at this pid now is not
        # our shell. Crashed, regardless of pid-existence.
        return CRASHED
    pid = entry.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return CRASHED
    if not has_controlling_tty(pid):
        return GHOST
    return LIVE
