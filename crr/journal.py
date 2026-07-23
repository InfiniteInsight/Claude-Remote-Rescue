"""Journal store: one JSON file per tracked shell session.

Layout (under the state dir):

    tabs/<pid>.json      -- live journal entries, schema v1
    archive/<name>.json  -- dismissed / given-up entries

Schema v1 (see DESIGN.md):

    {
      "v": 1,
      "pid": 12345,
      "boot_id": "...",
      "cwd": "/home/u/project",
      "host": "tab | tmux | ssh",
      "shell": "zsh | bash | fish",
      "claude": {"session_id": "...", "started": "...", "verified": true},
      "last_cmd": "...",
      "tmux_session": null,
      "updated": "ISO-8601",
      "revived": 0
    }

All writes are tmp-file+rename (atomic): a reader never observes a
partially written entry, and a crash mid-write leaves the previous entry
intact.

State dir resolution:
- $CRR_STATE_DIR when set (tests, and explicit overrides).
- macOS: ~/Library/Application Support/crr
- Linux: $XDG_STATE_HOME/crr, falling back to ~/.local/state/crr
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Paths


def state_dir() -> Path:
    override = os.environ.get("CRR_STATE_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "crr"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "crr"
    return Path.home() / ".local" / "state" / "crr"


def tabs_dir() -> Path:
    return state_dir() / "tabs"


def archive_dir() -> Path:
    return state_dir() / "archive"


def entry_path(pid: int) -> Path:
    return tabs_dir() / ("%d.json" % int(pid))


# ---------------------------------------------------------------------------
# Entry construction


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_entry(
    pid: int,
    cwd: str,
    shell: str,
    host: str,
    boot_id: str,
    *,
    claude: Optional[Dict] = None,
    last_cmd: Optional[str] = None,
    tmux_session: Optional[str] = None,
) -> Dict:
    return {
        "v": SCHEMA_VERSION,
        "pid": int(pid),
        "boot_id": boot_id,
        "cwd": cwd,
        "host": host,
        "shell": shell,
        "claude": claude,
        "last_cmd": last_cmd,
        "tmux_session": tmux_session,
        "updated": now_iso(),
        "revived": 0,
    }


# ---------------------------------------------------------------------------
# Atomic I/O


def _atomic_write_json(path: Path, data: Dict) -> None:
    """Write *data* to *path* via tmp-file+rename.

    On any failure the temp file is removed and the previous file (if any)
    is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_entry(entry: Dict) -> Path:
    entry["updated"] = now_iso()
    path = entry_path(entry["pid"])
    _atomic_write_json(path, entry)
    return path


def read_entry(pid: int) -> Optional[Dict]:
    return _read_json(entry_path(pid))


def _read_json(path: Path) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_entries() -> List[Dict]:
    """All readable journal entries, sorted by pid.

    Unparseable or half-typed files (never expected thanks to atomic
    writes, but the dir is user-visible) are skipped silently.
    """
    entries = []
    directory = tabs_dir()
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if isinstance(data, dict) and "pid" in data:
            entries.append(data)
    entries.sort(key=lambda e: e.get("pid", 0))
    return entries


def delete_entry(pid: int) -> bool:
    """Remove the entry file. Returns True when a file was removed."""
    try:
        os.unlink(entry_path(pid))
        return True
    except FileNotFoundError:
        return False


def archive_entry(pid: int, reason: str = "") -> Optional[Path]:
    """Move an entry into the archive/ area.

    The archived copy gains ``archived``/``archive_reason`` fields and a
    timestamped filename so successive archives of a recycled pid never
    collide. Returns the archive path, or None when no entry existed.
    """
    entry = read_entry(pid)
    if entry is None:
        return None
    entry["archived"] = now_iso()
    if reason:
        entry["archive_reason"] = reason
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = archive_dir() / ("%d-%s.json" % (int(pid), stamp))
    n = 0
    while dest.exists():
        n += 1
        dest = archive_dir() / ("%d-%s-%d.json" % (int(pid), stamp, n))
    _atomic_write_json(dest, entry)
    delete_entry(pid)
    return dest


def relaunch_flag_path(pid: int) -> Path:
    """Path of the kick relaunch flag for *pid*.

    [lesson: flag files] Written by ``ops.kick`` only when a kill actually
    lands; consumed atomically (check-and-clear) by the shim's repair
    loop, and cleared unread at the claude() wrapper's own start so a
    stale flag never silently resumes a session the user closed on
    purpose.
    """
    return state_dir() / "relaunch" / ("%d.flag" % int(pid))


def write_relaunch_flag(pid: int) -> Path:
    path = relaunch_flag_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def take_relaunch_flag(pid: int) -> bool:
    """Atomically check-and-clear the relaunch flag for *pid*.

    Returns True when a flag was present (and has now been removed),
    False when there was nothing to clear.
    """
    try:
        os.unlink(relaunch_flag_path(pid))
        return True
    except FileNotFoundError:
        return False


def list_archived() -> List[Dict]:
    entries = []
    directory = archive_dir()
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if isinstance(data, dict):
            entries.append(data)
    return entries
