"""Relaunch-flag store — the one bit of shared state between `ops.kick` and
the shim's repair loop.

A flag at ``<state_dir>/relaunch/<shell_pid>`` (content = the session id to
resume) means "this session was intentionally kicked; resume it silently".
Armed by kick only when the kill lands; cleared by the wrapper at start so a
flag from a session the user later closed on purpose never silently resumes
it. Pure core file I/O, consistent with journal.py (core owns the state-dir
filesystem); a flag is an opaque marker, so it needs no versioned contract.
"""

from __future__ import annotations

import os
from pathlib import Path


class FlagStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "relaunch"

    def _path(self, pid: int) -> Path:
        return self._dir / str(pid)

    def arm(self, pid: int, sid: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._path(pid)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(sid, encoding="utf-8")
        os.replace(tmp, target)  # atomic

    def read(self, pid: int) -> str | None:
        try:
            return self._path(pid).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None

    def clear(self, pid: int) -> None:
        self._path(pid).unlink(missing_ok=True)
