"""Relaunch/close flag store — the shared state between the kick/close ops
and the shim's repair loop.

A flag at ``<state_dir>/relaunch/<shell_pid>`` tells the wrapper what to do
after claude next exits:

- ``relaunch <sid>`` (armed by kick) → silently ``claude --resume <sid>``.
- ``close``           (armed by close) → ``claude-exit`` then ``exit`` the shell.

Absent → the wrapper offers on a crash. Armed only when a kill lands; cleared
by the wrapper at start so a flag never acts on a later launch. Pure core file
I/O, consistent with journal.py (core owns the state-dir filesystem); an
opaque per-pid marker, so it needs no versioned contract.
"""

from __future__ import annotations

import os
from pathlib import Path

RELAUNCH = "relaunch"
CLOSE = "close"


class FlagStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "relaunch"

    def _path(self, pid: int) -> Path:
        return self._dir / str(pid)

    def _write(self, pid: int, content: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._path(pid)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)  # atomic

    def arm_relaunch(self, pid: int, sid: str) -> None:
        self._write(pid, f"{RELAUNCH} {sid}")

    def arm_close(self, pid: int) -> None:
        self._write(pid, CLOSE)

    def read(self, pid: int) -> tuple[str, str | None] | None:
        try:
            content = self._path(pid).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        parts = content.split(None, 1)
        if not parts:
            return None
        sid = parts[1].strip() if len(parts) > 1 else None
        return (parts[0], sid)

    def clear(self, pid: int) -> None:
        self._path(pid).unlink(missing_ok=True)
