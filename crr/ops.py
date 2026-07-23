"""Session operations: kick, close, reopen, dismiss, remove.

Semantics (DESIGN.md):

- kick    -- restart claude in place, same conversation: kill the claude
             child of the journaled shell so the shim's repair loop
             resumes it. [lesson: kill-by-ancestry] Resumed sessions carry
             no --session-id on their argv, so the victim is found by
             process ancestry (children of the journaled shell pid via
             `ps -eo pid=,ppid=`) and killed as a process group -- never
             by cmdline pattern.
- close   -- remote equivalent of typing exit: hang up the shell.
- reopen  -- single-session revival of a crashed entry (delegates to the
             tmux reviver).
- dismiss -- clean up without restoring; archives crashed entries, hangs
             up and archives ghosts; refuses live sessions.
- remove  -- pure delist: deletes the journal entry, touches no process.

[lesson: recycled pids] Every destructive operation gates on the
classifier result -- never on bare pid-existence. A reboot-recycled pid
classifies as crashed (boot mismatch) and is refused any signal.

All operations return OpResult; failures propagate as distinct statuses
and exit codes, never swallowed.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Dict, List, Optional

from . import classify, journal
from .result import (
    EXIT_FAILED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_REFUSED,
    OpResult,
)


# ---------------------------------------------------------------------------
# Process-tree helpers (ancestry, never cmdline patterns)


def _process_table() -> Dict[int, List[int]]:
    """Map ppid -> [child pids] from `ps -eo pid=,ppid=` (portable)."""
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table: Dict[int, List[int]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table.setdefault(ppid, []).append(pid)
    return table


def _children_of(pid: int, table: Optional[Dict[int, List[int]]] = None) -> List[int]:
    if table is None:
        table = _process_table()
    return list(table.get(int(pid), []))


def _kill_pid(pid: int, sig: int) -> None:
    os.kill(int(pid), sig)


def _kill_group(pgid: int, sig: int) -> None:
    os.killpg(int(pgid), sig)


def _kill_child_groups(shell_pid: int, sig: int = signal.SIGTERM) -> List[int]:
    """Kill the process group of each direct child of *shell_pid*.

    The interactive shell puts its foreground job (the claude child) in
    its own process group; killing that group takes claude and all of its
    descendants down together. A child that shares the shell's own group
    (no job control) is killed individually so the shell survives to run
    its repair loop.

    Returns the list of pids/pgids signalled.
    """
    try:
        shell_pgid = os.getpgid(int(shell_pid))
    except OSError:
        shell_pgid = None
    signalled: List[int] = []
    for child in _children_of(shell_pid):
        try:
            pgid = os.getpgid(child)
        except OSError:
            continue
        try:
            if shell_pgid is not None and pgid == shell_pgid:
                _kill_pid(child, sig)
            else:
                _kill_group(pgid, sig)
        except OSError:
            continue
        signalled.append(child)
    return signalled


# ---------------------------------------------------------------------------
# Gate helper


def _load_and_classify(op: str, pid: int):
    """Return (entry, state, error_result). error_result is None when found."""
    entry = journal.read_entry(pid)
    if entry is None:
        return None, None, OpResult(
            op=op,
            pid=pid,
            ok=False,
            status="not-found",
            detail="no journal entry for pid %d" % pid,
            exit_code=EXIT_NOT_FOUND,
        )
    state = classify.classify(entry)
    return entry, state, None


def _refused(op: str, pid: int, state: str, why: str) -> OpResult:
    return OpResult(
        op=op,
        pid=pid,
        ok=False,
        status="refused-%s" % state,
        state=state,
        detail=why,
        exit_code=EXIT_REFUSED,
    )


# ---------------------------------------------------------------------------
# Operations


def kick(pid: int) -> OpResult:
    """Kill the claude child of the journaled shell (by ancestry) so the
    shim's repair loop restarts the same conversation in place.

    [lesson: flag files] The relaunch flag is written only once the kill
    has actually landed (a signalled child group) -- never speculatively
    -- so the shim's repair loop only fires for kicks that really
    happened. The shim clears any stale flag itself at the claude()
    wrapper's own start.
    """
    entry, state, err = _load_and_classify("kick", pid)
    if err:
        return err
    if state == classify.CRASHED:
        return _refused(
            "kick",
            pid,
            state,
            "entry is crashed (pid dead or boot mismatch); refusing to signal"
            " a possibly-recycled pid",
        )
    signalled = _kill_child_groups(pid, signal.SIGTERM)
    if not signalled:
        return OpResult(
            op="kick",
            pid=pid,
            ok=False,
            status="no-child",
            state=state,
            detail="shell has no child processes to kick",
            exit_code=EXIT_FAILED,
        )
    journal.write_relaunch_flag(pid)
    return OpResult(
        op="kick",
        pid=pid,
        ok=True,
        status="kicked",
        state=state,
        detail="signalled %d child process group(s)" % len(signalled),
        extra={"signalled": signalled},
    )


def close(pid: int) -> OpResult:
    """Remote equivalent of typing exit: SIGHUP the journaled shell.

    The shim's exit hook is responsible for deregistering the entry."""
    entry, state, err = _load_and_classify("close", pid)
    if err:
        return err
    if state == classify.CRASHED:
        return _refused(
            "close",
            pid,
            state,
            "entry is crashed; refusing to signal a possibly-recycled pid",
        )
    try:
        _kill_pid(pid, signal.SIGHUP)
    except OSError as exc:
        return OpResult(
            op="close",
            pid=pid,
            ok=False,
            status="signal-failed",
            state=state,
            detail=str(exc),
            exit_code=EXIT_FAILED,
        )
    return OpResult(op="close", pid=pid, ok=True, status="closed", state=state)


def reopen(pid: int) -> OpResult:
    """Single-session revival: revive one crashed entry into tmux."""
    from . import revive as revive_mod  # local import: revive depends on ops types only

    entry, state, err = _load_and_classify("reopen", pid)
    if err:
        return err
    if state != classify.CRASHED:
        return _refused(
            "reopen",
            pid,
            state,
            "entry is %s, not crashed; nothing to reopen" % state,
        )
    result = revive_mod.revive_entry(entry)
    result.op = "reopen"
    return result


def dismiss(pid: int) -> OpResult:
    """Clean up without restoring.

    crashed -> archive the entry.
    ghost   -> hang up the orphaned shell, then archive.
    live    -> refused (a healthy session is not cleanup material).
    """
    entry, state, err = _load_and_classify("dismiss", pid)
    if err:
        return err
    if state == classify.LIVE:
        return _refused(
            "dismiss", pid, state, "session is live; use close or remove instead"
        )
    if state == classify.GHOST:
        try:
            _kill_pid(pid, signal.SIGHUP)
        except OSError:
            pass  # already gone is fine; archiving is the point
    dest = journal.archive_entry(pid, reason="dismissed (%s)" % state)
    if dest is None:
        return OpResult(
            op="dismiss",
            pid=pid,
            ok=False,
            status="not-found",
            state=state,
            detail="entry vanished before archive",
            exit_code=EXIT_NOT_FOUND,
        )
    return OpResult(
        op="dismiss",
        pid=pid,
        ok=True,
        status="dismissed",
        state=state,
        extra={"archived_to": str(dest)},
    )


def remove(pid: int) -> OpResult:
    """Pure delist: delete the journal entry, touch nothing else."""
    removed = journal.delete_entry(pid)
    if not removed:
        return OpResult(
            op="remove",
            pid=pid,
            ok=False,
            status="not-found",
            detail="no journal entry for pid %d" % pid,
            exit_code=EXIT_NOT_FOUND,
        )
    return OpResult(op="remove", pid=pid, ok=True, status="removed")


# ---------------------------------------------------------------------------
# Status assembly (consumed by the CLI now, the web layer later)


def status() -> List[Dict]:
    """All journal entries with their computed classifier state attached."""
    from . import bootid

    current = bootid.current_boot_id()
    out = []
    for entry in journal.list_entries():
        item = dict(entry)
        item["state"] = classify.classify(entry, current)
        out.append(item)
    return out


def gc(archive_retention_days: int = 30) -> Dict:
    """Garbage-collect the journal.

    - Crashed entries with no claude session id (nothing revivable) are
      archived.
    - Archive files older than *archive_retention_days* are deleted.
    - Stray .tmp files from interrupted writes are removed.
    """
    import time

    archived = 0
    for entry in journal.list_entries():
        if classify.classify(entry) != classify.CRASHED:
            continue
        claude_info = entry.get("claude") or {}
        if not claude_info.get("session_id"):
            if journal.archive_entry(entry["pid"], reason="gc: crashed, no sid"):
                archived += 1
    pruned = 0
    cutoff = time.time() - archive_retention_days * 86400
    adir = journal.archive_dir()
    if adir.is_dir():
        for path in adir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    pruned += 1
            except OSError:
                continue
    tmp_removed = 0
    tdir = journal.tabs_dir()
    if tdir.is_dir():
        for path in tdir.glob("*.tmp"):
            try:
                path.unlink()
                tmp_removed += 1
            except OSError:
                continue
    return {"archived": archived, "pruned": pruned, "tmp_removed": tmp_removed}
