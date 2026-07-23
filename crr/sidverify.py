"""Sid guessing + re-verification for bare `claude --resume` (picker).

DESIGN.md: bare `--resume` launches (the picker) get a *guessed* session
id journaled -- the newest transcript for the launch cwd at launch time
-- marked ``verified: false``. After the claude child has been alive
~10s (past the picker), the wrapper resolves the project transcript dir
again and takes the ``*.jsonl`` file most recently modified *after* the
child's start time as the authoritative sid. If nothing newer shows up
(picker still open, or resume aborted) the guess is kept unverified and
revival for that entry falls back to bare `--resume` rather than
resuming a possibly-wrong sid (see ``revive.build_claude_argv``).

Both the initial guess (``guess_sid`` / ``newest_transcript``) and the
re-verify (``verify_sid`` / ``newest_transcript_after``) are exposed as
plumbing CLI subcommands (``crr guess-sid``, ``crr verify-sid``) so the
shell shims stay dependency-free -- all matching logic lives here in
Python, fully unit-testable against fixture transcript directories.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from . import journal

DEFAULT_WAIT_SECONDS = 10.0


def claude_projects_dir() -> Path:
    """Where Claude Code keeps per-project transcript directories.

    ``$CRR_CLAUDE_PROJECTS_DIR`` overrides for tests, matching the
    override pattern used by ``journal.state_dir``.
    """
    override = os.environ.get("CRR_CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects"


def project_slug(cwd: str) -> str:
    """Slugify a cwd the way Claude Code names its transcript directory."""
    return cwd.replace("/", "-")


def newest_transcript_after(cwd: str, after_epoch: float) -> Optional[Path]:
    """The ``*.jsonl`` transcript for *cwd* with the newest mtime, provided
    that mtime is strictly after *after_epoch*. None when the project
    directory is missing or nothing qualifies."""
    directory = claude_projects_dir() / project_slug(cwd)
    if not directory.is_dir():
        return None
    best: Optional[Path] = None
    best_mtime = after_epoch
    for path in directory.glob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best = path
    return best


def newest_transcript(cwd: str) -> Optional[Path]:
    """The newest ``*.jsonl`` transcript for *cwd*, regardless of mtime."""
    return newest_transcript_after(cwd, after_epoch=float("-inf"))


def guess_sid(cwd: str) -> str:
    """Best-effort sid guess at launch time (``crr guess-sid``): the
    basename (sans extension) of the newest transcript for *cwd*, or ""
    when there is none yet."""
    path = newest_transcript(cwd)
    return path.stem if path is not None else ""


def verify_sid(
    pid: int, started_epoch: float, wait_seconds: float = DEFAULT_WAIT_SECONDS
) -> bool:
    """Wait past the picker window, then verify or keep the guessed sid.

    Returns True when a transcript newer than *started_epoch* was found
    and journaled as the authoritative, verified sid; False when the
    guess is left as-is (picker still open, resume aborted, or the
    journal entry vanished in the meantime).
    """
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    entry = journal.read_entry(pid)
    if entry is None:
        return False
    cwd = entry.get("cwd")
    if not cwd:
        return False
    newest = newest_transcript_after(cwd, started_epoch)
    if newest is None:
        return False
    sid = newest.stem
    claude_info = entry.get("claude") or {}
    claude_info["session_id"] = sid
    claude_info["verified"] = True
    claude_info.setdefault("started", journal.now_iso())
    entry["claude"] = claude_info
    journal.write_entry(entry)
    return True
