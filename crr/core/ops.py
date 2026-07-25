"""Session operations — the single classifier-gated home.

reopen / dismiss / remove orchestration lives here, not in the CLI, so the
CLI handlers and the web POST endpoint call the *same* implementation. A
gate that drifts between the two surfaces is exactly the recycled-pid
hazard the DESIGN warns about (every destructive op gates on the
classifier, never bare pid-existence).

Pure core: takes the journal/archive stores and the BootIdentity/
ProcessProbe/TmuxSpawner ports, so it is fully testable with fakes.
kick/close (which signal live processes) are deliberately not here yet.
"""

from __future__ import annotations

from typing import NamedTuple

from crr.core import contracts
from crr.core.archive import ArchiveStore
from crr.core.classifier import CRASHED, classify
from crr.core.journal import JournalStore
from crr.core.ports import BootIdentity, ProcessProbe, TabSpawner, TmuxSpawner
from crr.core.reviver import attach_argv, revival_argv, session_name


class OpResult(NamedTuple):
    ok: bool
    message: str


def remove(store: JournalStore, pid: int) -> OpResult:
    """Pure delist — forget the session, touch nothing else. Idempotent."""
    store.remove(pid)
    return OpResult(True, f"removed {pid}")


def dismiss(
    store: JournalStore,
    archive: ArchiveStore,
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    now: str,
) -> OpResult:
    """Clean up a CRASHED session: archive a claude-bearing one, then delist."""
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    state = classify(entry, boot, probe)
    if state != CRASHED:
        return OpResult(False, f"session {pid} is {state}, not crashed — refusing")
    if entry.get("claude") is not None:
        archive.archive(entry, "dismissed", now)
    store.remove(pid)
    return OpResult(True, f"dismissed {pid}")


def reopen(
    store: JournalStore,
    tmux: TmuxSpawner,
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    now: str,
    *,
    tab_spawner: TabSpawner | None = None,
) -> OpResult:
    """Revive one CRASHED claude session on demand (no strike accounting).

    Revival always lands in a detached tmux session first (durable). If a
    ``tab_spawner`` is available, a visible tab then attaches to it — a
    best-effort convenience: a tab failure is surfaced in the message but
    never demotes the successful revival to a failure.
    """
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    if entry.get("claude") is None:
        return OpResult(False, f"session {pid} has no claude session to resume")
    state = classify(entry, boot, probe)
    if state != CRASHED:
        return OpResult(False, f"session {pid} is {state}, not crashed — refusing")

    name = session_name(entry)
    if name in tmux.list_sessions():
        base = f"already running as {name}"
    else:
        tmux.new_detached_session(name, entry["cwd"], revival_argv(entry))
        entry["tmux_session"] = name
        entry["updated"] = now
        store.write(entry)
        base = f"reopened {pid} as {name}"
    return OpResult(True, base + _open_tab(tab_spawner, name))


def _open_tab(tab_spawner: TabSpawner | None, name: str) -> str:
    """Best-effort visible tab attaching to ``name``; returns a message suffix.

    The tmux revival is already durable by the time this runs, so any
    failure here is convenience-only — reported, never fatal.
    """
    if tab_spawner is None or not tab_spawner.available():
        return ""
    try:
        tab_spawner.open_tab(attach_argv(name))
        return " (opened in a new tab)"
    except Exception as exc:  # best-effort: an osascript/subprocess failure
        return f" (tab spawn failed: {exc})"
