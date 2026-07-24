"""State-dir path adapter.

DESIGN.md: ``$XDG_STATE_HOME/crr`` (Linux, default ``~/.local/state/crr``)
and ``~/Library/Application Support/crr`` (macOS). ``resolve`` is a pure
function of (system, env, home) so it is testable without touching the
real environment; ``state_dir()`` is the thin wrapper the composition
root calls.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Mapping


def resolve(system: str, env: Mapping[str, str], home: Path) -> Path:
    if system == "Darwin":
        return home / "Library" / "Application Support" / "crr"
    # Linux and other POSIX: honor XDG, else the spec default.
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else home / ".local" / "state"
    return base / "crr"


def state_dir() -> Path:
    return resolve(platform.system(), os.environ, Path.home())
