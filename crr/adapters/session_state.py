"""Claude Code's own per-process session state (spec 2026-08-09, Phase 1).

Claude Code writes `~/.claude/sessions/<pid>.json` for each running
process and updates it on every state change. The field that matters here
is ``bridgeSessionId``: non-null while the phone's Remote Control link is
up, null when it is down.

That it is authoritative was established by reading the shipped bundle,
not inferred. The bridge session lives in one module-level variable with
exactly one setter; that setter writes this field on every change, and
teardown calls it with null. The app's own user-facing copy defines
"connected" as the same variable.

Undocumented internal state, so every read degrades: a missing directory,
a corrupt file, or a missing field yields an absent entry or
``field_present=False`` — never a fabricated value.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, NamedTuple


class SessionState(NamedTuple):
    pid: int | None
    bridge_session_id: str | None
    #: There is a READABLE answer about the bridge in this file — the key is
    #: present AND its value is one this module understands (a string, or
    #: null meaning "the link is down"). False covers both the key being
    #: absent (an older Claude Code, or a renamed field) and its value being
    #: a shape this module cannot read; core turns either into "unknown".
    field_present: bool
    status: str | None
    waiting_for: str


def _bridge(data: dict) -> tuple[str | None, bool]:
    """Split ``bridgeSessionId`` into ``(value, readable)``.

    A null bridgeSessionId is an ANSWER ("the link is down"), so it must
    stay distinguishable from the field being absent entirely — hence the
    separate flag rather than folding both into ``None``.

    Anything that is neither a string nor null is NOT an answer. If a future
    Claude Code reshapes the field (to ``{"id": ...}``, say), reporting it as
    a bare ``None`` with the flag set would have core classify the session
    ``unreachable`` and the watchdog would restart a live process on the
    strength of a value it could not parse. Unreadable degrades to unknown,
    in keeping with every other failure route here.
    """
    if "bridgeSessionId" not in data:
        return None, False
    value = data["bridgeSessionId"]
    if value is None:
        return None, True
    if isinstance(value, str):
        return value, True
    return None, False


def read_all(home: Path | None = None) -> dict[str, SessionState]:
    """Newest state file per session id, as ``{session_id: SessionState}``.

    ONE directory scan, not one per card: the caller resolves this once per
    poll and injects the map. Newest-by-mtime wins because a session id can
    have several files from successive claude processes — observed live with
    nineteen for one id — and only the newest describes the running one.

    No liveness filter: every session id with a state file is returned, dead
    pids included (117 of 133 files on the author's machine). Whether the
    newest file's pid is one of a session's live claude processes is the
    caller's ``pid_matched``, decided against the process table, not here.
    """
    home = home or Path.home()
    sessions_dir = home / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return {}
    newest: dict[str, tuple[float, SessionState]] = {}
    for path in sessions_dir.glob("*.json"):
        try:
            mtime = path.stat().st_mtime
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue          # corrupt or unreadable: skip this file only
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        if sid in newest and mtime <= newest[sid][0]:
            continue
        pid = data.get("pid")
        status = data.get("status")
        waiting_for = data.get("waitingFor")
        bridge, readable = _bridge(data)
        newest[sid] = (mtime, SessionState(
            pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            bridge_session_id=bridge,
            field_present=readable,
            status=status if isinstance(status, str) else None,
            waiting_for=waiting_for if isinstance(waiting_for, str) else "",
        ))
    return {sid: state for sid, (_m, state) in newest.items()}


def archive_stale(
    *,
    home: Path | None = None,
    is_alive: Callable[[int], bool],
) -> int:
    """Move state files whose pid is dead to ``sessions/archive/``.

    Returns the number of files archived. Silently skips non-numeric
    filenames and files that fail to move.
    """
    home = home or Path.home()
    sessions_dir = home / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return 0
    archived = 0
    for path in sessions_dir.glob("*.json"):
        stem = path.stem
        try:
            pid = int(stem)
        except ValueError:
            continue
        if is_alive(pid):
            continue
        archive_dir = sessions_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
        dest = archive_dir / path.name
        try:
            os.replace(path, dest)
            archived += 1
        except OSError:
            continue
    return archived
