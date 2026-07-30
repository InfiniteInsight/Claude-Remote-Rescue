"""Session operations — the single classifier-gated home.

reopen / dismiss / remove orchestration lives here, not in the CLI, so the
CLI handlers and the web POST endpoint call the *same* implementation. A
gate that drifts between the two surfaces is exactly the recycled-pid
hazard the DESIGN warns about (every destructive op gates on the
classifier, never bare pid-existence).

Pure core: takes the journal/archive stores and the BootIdentity/
ProcessProbe/TmuxSpawner ports, so it is fully testable with fakes.
kick/close (which signal live processes) live here too, gated the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from crr.core import contracts
from crr.core.archive import ArchiveStore
from crr.core.classifier import CRASHED, classify
from crr.core.journal import JournalStore
from crr.core.ports import BootIdentity, ProcessProbe, TabSpawner, TmuxSpawner
from crr.core.reviver import attach_argv, revival_argv, session_name

if TYPE_CHECKING:
    from crr.core.flags import FlagStore
    from crr.core.ports import ProcessController


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


def close(
    store: JournalStore,
    controller: "ProcessController",
    flags: "FlagStore",
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    *,
    grace: float,
) -> OpResult:
    """End a LIVE/GHOST session (remote `exit`): arm the close flag, then
    SIGTERM each claude group (escalating to SIGKILL after the grace window).
    The wrapper (repair loop) sees the close flag and exits the shell, so the
    terminal closes and the card clears. The flag survives whenever at least
    one group kill lands — a partial failure across several claude groups
    must not roll back a flag whose claude is already gone; it is rolled
    back only when every kill fails."""
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    state = classify(entry, boot, probe)
    if state == CRASHED:
        return OpResult(False, f"session {pid} is crashed, not running — refusing")
    groups = controller.claude_groups(pid)
    if not groups:
        return OpResult(False, f"session {pid}: no running claude process found")
    flags.arm_close(pid)
    landed, errors = 0, []
    for pgid in groups:
        try:
            controller.terminate_group(pgid, grace)
            landed += 1
        except OSError as exc:
            errors.append(str(exc))
    if landed == 0:
        flags.clear(pid)  # no kill landed -> the flag must not linger
        return OpResult(False, f"close {pid} failed to signal: {'; '.join(errors)}")
    suffix = f" ({len(errors)} claude group(s) failed to signal: {'; '.join(errors)})" if errors else ""
    return OpResult(True, f"closed {pid}{suffix}")


def kick(
    store: JournalStore,
    controller: "ProcessController",
    flags: "FlagStore",
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    *,
    grace: float,
) -> OpResult:
    """Restart claude in place on the same conversation: arm the relaunch
    flag, then SIGTERM/grace/SIGKILL each claude group. The flag survives
    whenever at least one group kill lands — a partial failure across
    several claude groups must not roll back a flag whose claude is already
    gone; it is rolled back only when every kill fails, so the shim never
    resumes a kick that did not happen."""
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    if entry.get("claude") is None:
        return OpResult(False, f"session {pid} has no claude session to relaunch")
    state = classify(entry, boot, probe)
    if state == CRASHED:
        return OpResult(False, f"session {pid} is crashed, not running — use reopen")
    groups = controller.claude_groups(pid)
    if not groups:
        return OpResult(False, f"session {pid}: no running claude process found")
    flags.arm_relaunch(pid, entry["claude"]["session_id"])
    landed, errors = 0, []
    for pgid in groups:
        try:
            controller.terminate_group(pgid, grace)
            landed += 1
        except OSError as exc:
            errors.append(str(exc))
    if landed == 0:
        flags.clear(pid)  # no kill landed -> the flag must not linger
        return OpResult(False, f"kick {pid} failed to signal: {'; '.join(errors)}")
    suffix = f" ({len(errors)} claude group(s) failed to signal: {'; '.join(errors)})" if errors else ""
    return OpResult(True, f"kicked {pid} (resuming the same conversation){suffix}")


def detmux(
    store: JournalStore,
    archive: ArchiveStore,
    tmux: TmuxSpawner,
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    now: str,
    *,
    tab_spawner: TabSpawner | None,
) -> OpResult:
    """Re-home a revived (detached-tmux) session into a visible tab.

    Classifier-gated like every other session op here (DESIGN: all session
    ops are classifier-gated) — a card can carry ``tmux_session`` while
    being LIVE (same-boot pid preservation across a same-boot restart), and
    detmux must refuse that rather than archive+delist a live shell out of
    crr's management.

    Opens a tab attached to the stored ``tmux_session`` name, then takes
    the entry out of crr's management entirely (archive + delist) rather
    than merely clearing the field. ``tmux_session`` is owned by the
    reviver: its reset branch would re-park a cleared field within one
    watchdog pass, and would later resurrect the conversation once the
    user exits claude in the attached tab. Delisting removes the entry
    from the reviver's domain for good; archiving (mirroring ``dismiss``)
    keeps provenance for any entry that still carries a claude session.

    Liveness of the tmux session itself comes from tmux, never the stored
    field (reviver lesson). Unlike reopen — where the tab is a best-effort
    convenience on an already-durable revival — the tab IS this operation:
    no spawner is a refusal, and a spawn failure leaves the bookkeeping
    untouched so the card keeps offering the button.
    """
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    state = classify(entry, boot, probe)
    if state != CRASHED:
        return OpResult(False, f"session {pid} is {state}, not crashed — refusing "
                               "(detmux re-homes revived sessions only)")
    name = entry.get("tmux_session")
    if not name:
        return OpResult(False, f"session {pid} is not tmux-parked")
    if name not in tmux.list_sessions():
        return OpResult(False, f"tmux session {name} is gone")
    if tab_spawner is None or not tab_spawner.available():
        return OpResult(False, "no terminal tab spawner available on this host")
    try:
        tab_spawner.open_tab(attach_argv(name))
    except Exception as exc:  # adapter subprocess/osascript failure
        return OpResult(False, f"detmux {pid} failed to open a tab: {exc}")
    if entry.get("claude") is not None:
        archive.archive(entry, "detmuxed", now)
    store.remove(pid)
    return OpResult(True, f"de-tmuxed {pid}: attached {name} in a tab; crr no longer manages it")


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
