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
from crr.core.classifier import CRASHED, GHOST, LIVE, classify
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
    archive: ArchiveStore,
    tmux: TmuxSpawner,
    controller: "ProcessController",
    flags: "FlagStore",
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    now: str,
    *,
    grace: float,
    tab_spawner: TabSpawner | None = None,
) -> OpResult:
    """Revive a session on demand, dispatching on the classifier state.

    - CRASHED: the original no-strike-accounting revival — spawn (or note
      already-running), unchanged.
    - GHOST: [user request 2026-07-30] the mobile rescue path. Close on a
      ghost destroys revival data (the wrapper's close branch runs
      claude-exit -> claude=None -> deregister), and there is otherwise no
      way from a phone to get a ghost's conversation into tmux. Close-flag
      the orphan wrapper so it exits its shell instead of silently
      auto-resuming (the no-tty->resume rule would otherwise spawn a
      duplicate claude on the same sid), kill claude's group(s), archive
      the entry as ``"ghost-restored"`` *before* any spawn attempt (so
      revival data survives every later failure), delist it, then spawn
      the detached tmux revival. See ``_reopen_ghost`` for the full
      kill-first-then-preserve-then-spawn ordering and its safety
      rationale.
    - LIVE: refused — kick/close are the ops for a running claude.

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

    if state == LIVE:
        return OpResult(False, f"session {pid} is live — use kick or close")

    if state == GHOST:
        return _reopen_ghost(
            store, archive, tmux, controller, flags, entry, pid, now,
            grace=grace, tab_spawner=tab_spawner,
        )

    # CRASHED — original path, unchanged.
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


def _reopen_ghost(
    store: JournalStore,
    archive: ArchiveStore,
    tmux: TmuxSpawner,
    controller: "ProcessController",
    flags: "FlagStore",
    entry: dict,
    pid: int,
    now: str,
    *,
    grace: float,
    tab_spawner: TabSpawner | None,
) -> OpResult:
    """The GHOST branch of ``reopen`` (see its docstring for the "why").

    Ordering, each choice load-bearing:

    1. Kill first. If claude groups exist, arm the close flag then signal
       them with the same landed/errors accounting as kick/close — if NO
       kill lands, the flag is rolled back and the op fails with the entry
       untouched (a flag must survive only when a kill actually landed).
       If no groups exist claude is already dead: no flag, nothing to kill
       (never arm a flag without a landing kill). Killing before archiving
       means a kill failure leaves nothing archived to roll back.
    2. Preserve second, before any spawn attempt: archive the entry with
       reason ``"ghost-restored"`` and ``tmux_session`` set to
       ``session_name(entry)``, then delist it. This makes the archive
       record durable before the spawn is ever attempted, so a spawn
       failure can never lose the conversation.
    3. Spawn last (kill-first ordering avoids two claudes sharing a sid). A
       spawn failure is reported honestly, but the op still succeeds at
       preservation: the ``"ghost-restored"`` archive record is a revival
       candidate for the watchdog (not in the reviver's terminal-reasons
       skip tuple), so it is revived within one pass regardless.
    """
    groups = controller.claude_groups(pid)
    kill_suffix = ""
    if groups:
        flags.arm_close(pid)
        landed, errors = _signal_groups(controller, groups, grace)
        if landed == 0:
            flags.clear(pid)  # no kill landed -> the flag must not linger
            return OpResult(False, f"reopen {pid} failed to signal: {'; '.join(errors)}")
        if errors:
            kill_suffix = f" ({len(errors)} claude group(s) failed to signal: {'; '.join(errors)})"

    name = session_name(entry)
    entry["tmux_session"] = name
    archive.archive(entry, "ghost-restored", now)
    store.remove(pid)

    if name in tmux.list_sessions():
        return OpResult(
            True, f"restored {pid}'s conversation as {name} (already running){kill_suffix}"
            + _open_tab(tab_spawner, name)
        )
    try:
        tmux.new_detached_session(name, entry["cwd"], revival_argv(entry))
    except Exception as exc:  # adapter subprocess failure
        return OpResult(
            True,
            f"restored {pid}'s conversation to the archive as ghost-restored, but the "
            f"tmux spawn failed ({exc}) — the watchdog will revive it on its next "
            f"pass{kill_suffix}",
        )
    return OpResult(
        True, f"restored {pid}'s conversation into detached tmux as {name}{kill_suffix}"
        + _open_tab(tab_spawner, name)
    )


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
    landed, errors = _signal_groups(controller, groups, grace)
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
    landed, errors = _signal_groups(controller, groups, grace)
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


def _signal_groups(
    controller: "ProcessController", groups: list[int], grace: float
) -> tuple[int, list[str]]:
    """SIGTERM/grace/SIGKILL each claude process group; tally landed vs.
    failed. Shared by kick/close/reopen's GHOST branch — three copies of
    this loop would be verbatim duplication (Task 3's landed/errors
    accounting, extracted)."""
    landed, errors = 0, []
    for pgid in groups:
        try:
            controller.terminate_group(pgid, grace)
            landed += 1
        except OSError as exc:
            errors.append(str(exc))
    return landed, errors


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
