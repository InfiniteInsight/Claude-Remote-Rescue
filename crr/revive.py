"""tmux reviver: bring crashed sessions back as detached tmux sessions.

A crashed entry with a Claude session id revives as a detached tmux
session running `claude --resume <sid>`; tab adapters (later phases) then
attach visibly where tabs exist.

[lesson: word-form exec] Commands handed to tmux MUST be argv word-form
(list arguments to `tmux new-session`), never a shell string -- otherwise
tmux wraps them in the login shell and journaling double-registers.

[lesson: give-up guard] A revived session that dies again is archived,
not re-revived forever. The journal entry carries a ``revived`` counter;
an entry that has already been revived once and crashed again is archived
instead of re-revived.

Sid re-verification (DESIGN.md): entries whose sid was guessed after a
picker-resume are journaled with ``"verified": false``. Reviving such an
entry with the guessed sid could resume the wrong conversation, so it is
revived with bare `claude --resume` (the picker) instead.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Dict, List, Optional

from . import classify, journal
from .result import (
    EXIT_FAILED,
    EXIT_GAVE_UP,
    EXIT_NO_TMUX,
    EXIT_REFUSED,
    OpResult,
)


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def tmux_session_name(entry: Dict) -> str:
    return "crr-%d" % int(entry["pid"])


def build_claude_argv(entry: Dict) -> Optional[List[str]]:
    """The claude command (argv word-form) reviving *entry*, or None when
    the entry has no session id to resume."""
    claude_info = entry.get("claude") or {}
    sid = claude_info.get("session_id")
    if not sid:
        return None
    if claude_info.get("verified", True):
        return ["claude", "--resume", str(sid)]
    # Unverified (guessed) sid: fall back to the picker rather than
    # resuming a possibly-wrong conversation.
    return ["claude", "--resume"]


def _spawn_tmux(argv: List[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def revive_entry(entry: Dict) -> OpResult:
    """Revive one crashed entry as a detached tmux session.

    The caller is responsible for only passing crashed entries (reopen and
    revive_all gate on the classifier); this function additionally applies
    the give-up guard and the unverified-sid fallback.
    """
    pid = int(entry["pid"])

    if int(entry.get("revived") or 0) >= 1:
        dest = journal.archive_entry(pid, reason="give-up: revived once and died again")
        return OpResult(
            op="revive",
            pid=pid,
            ok=False,
            status="gave-up",
            state=classify.CRASHED,
            detail="already revived once and crashed again; archived instead"
            " of re-reviving",
            exit_code=EXIT_GAVE_UP,
            extra={"archived_to": str(dest) if dest else None},
        )

    argv_tail = build_claude_argv(entry)
    if argv_tail is None:
        return OpResult(
            op="revive",
            pid=pid,
            ok=False,
            status="no-sid",
            state=classify.CRASHED,
            detail="entry has no claude session id; nothing to resume",
            exit_code=EXIT_REFUSED,
        )

    if not tmux_available():
        return OpResult(
            op="revive",
            pid=pid,
            ok=False,
            status="no-tmux",
            state=classify.CRASHED,
            detail="tmux not found on PATH; revival requires tmux",
            exit_code=EXIT_NO_TMUX,
        )

    name = tmux_session_name(entry)
    argv = ["tmux", "new-session", "-d", "-s", name]
    cwd = entry.get("cwd")
    if cwd:
        argv += ["-c", cwd]
    argv += argv_tail  # word-form: each claude arg is its own argv element

    try:
        proc = _spawn_tmux(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return OpResult(
            op="revive",
            pid=pid,
            ok=False,
            status="spawn-failed",
            state=classify.CRASHED,
            detail=str(exc),
            exit_code=EXIT_FAILED,
        )
    if proc.returncode != 0:
        return OpResult(
            op="revive",
            pid=pid,
            ok=False,
            status="tmux-failed",
            state=classify.CRASHED,
            detail=(proc.stderr or "").strip() or "tmux exited %d" % proc.returncode,
            exit_code=EXIT_FAILED,
        )

    entry["revived"] = int(entry.get("revived") or 0) + 1
    entry["tmux_session"] = name
    journal.write_entry(entry)
    return OpResult(
        op="revive",
        pid=pid,
        ok=True,
        status="revived",
        state=classify.CRASHED,
        detail="revived as detached tmux session %s" % name,
        extra={"tmux_session": name, "argv": argv},
    )


def revive_all() -> List[OpResult]:
    """Revive every crashed entry that has a claude session id.

    Entries without a sid are skipped silently (gc archives them); the
    give-up guard archives repeat offenders.
    """
    results: List[OpResult] = []
    for entry in journal.list_entries():
        if classify.classify(entry) != classify.CRASHED:
            continue
        claude_info = entry.get("claude") or {}
        if not claude_info.get("session_id"):
            continue
        results.append(revive_entry(entry))
    return results
