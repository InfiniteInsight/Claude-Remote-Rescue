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
from crr.core.ports import BootIdentity, ProcessProbe, TabSpawner, TabSpawnTimeout, TmuxSpawner
from crr.core.reviver import attach_argv, resolved_session_name, revival_argv

if TYPE_CHECKING:
    from crr.core.flags import FlagStore
    from crr.core.ports import ProcessController


class OpResult(NamedTuple):
    ok: bool
    message: str
    # The op did what it was asked, but not everything the user clicked for.
    # Today's only case: a revival that produced no visible tab on a host that
    # HAS tabs. The session is alive and attachable, so ok stays True — but
    # "reopen" means "put it in front of me", and a caller that renders this
    # as a plain success is lying by omission ([user request, 2026-08-09]).
    # Defaulted so every other op constructs OpResult(ok, message) unchanged.
    degraded: bool = False


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
    remote_control: bool,
    tab_spawner: TabSpawner | None = None,
    # Whether THIS host has a concept of visible tabs at all. Selecting the
    # adapter is crr.cli's job, so the discrimination between "headless, tabs
    # were never possible" and "tab-capable host, the tab did not appear"
    # arrives as a plain bool rather than core learning what WSL is.
    tabs_expected: bool = False,
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

    Revival always lands in a detached tmux session first (durable), then a
    visible tab attaches to it. The tab is part of what "reopen" means, not a
    garnish: when ``tabs_expected`` and no tab appears, the result is marked
    ``degraded`` so callers can say so ([user request, 2026-08-09]). ``ok``
    still stays True — the session is alive and attachable, and claiming
    otherwise would send the user hunting for a session that is right there.
    """
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    if entry.get("claude") is None:
        return OpResult(False, f"session {pid} has no claude session to resume")
    state = classify(entry, boot, probe)

    # F16 tri-state: resolve liveness ONCE, before any destructive step —
    # the GHOST branch kills + archives before it would otherwise learn
    # whether the target tmux session already exists, and a kill can't be
    # undone, so an unknown state must refuse here rather than mid-branch.
    live = tmux.list_sessions()
    if live is None:
        return OpResult(False, f"reopen {pid}: cannot determine tmux state — is tmux responding?")

    if state == LIVE:
        # A LIVE entry is normally a shell the user is working in, and
        # reopen would race a spawn against it. A PARKED one is different:
        # the journaled pid IS the process in the tmux session (#58), so
        # there is nothing to race and reopen means what the user expects —
        # attach a tab to it. Anything else live is still refused.
        name = entry.get("tmux_session")
        if not (name and name in live and tmux.session_pid(name) == pid):
            return OpResult(False, f"session {pid} is live — use kick or close")

    if state == GHOST:
        return _reopen_ghost(
            store, archive, tmux, controller, flags, entry, pid, now,
            live=live, grace=grace, remote_control=remote_control, tab_spawner=tab_spawner,
            tabs_expected=tabs_expected,
        )

    # CRASHED — original path, unchanged.
    name = resolved_session_name(entry)
    if name in live:
        base = f"already running as {name}"
    else:
        tmux.new_detached_session(
            name, entry["cwd"], revival_argv(entry, remote_control=remote_control)
        )
        entry["tmux_session"] = name
        entry["updated"] = now
        store.write(entry)
        base = f"reopened {pid} as {name}"
    suffix, landed = _open_tab(tab_spawner, name)
    return OpResult(True, base + suffix, degraded=tabs_expected and not landed)


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
    live: set[str],
    grace: float,
    remote_control: bool,
    tab_spawner: TabSpawner | None,
    tabs_expected: bool,
) -> OpResult:
    """The GHOST branch of ``reopen`` (see its docstring for the "why").

    ``live`` is the tmux liveness snapshot ``reopen`` already resolved
    (and confirmed non-None) before dispatching here — this branch commits
    irreversible steps (kill, archive) before it would otherwise learn
    whether the target session already exists, so the None-liveness
    refusal has to happen in the caller, not here.

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
       ``resolved_session_name(entry)``, then delist it. This makes the archive
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

    name = resolved_session_name(entry)
    entry["tmux_session"] = name
    archive.archive(entry, "ghost-restored", now)
    store.remove(pid)

    if name in live:
        suffix, landed = _open_tab(tab_spawner, name)
        return OpResult(
            True, f"restored {pid}'s conversation as {name} (already running){kill_suffix}"
            + suffix, degraded=tabs_expected and not landed
        )
    try:
        tmux.new_detached_session(
            name, entry["cwd"], revival_argv(entry, remote_control=remote_control)
        )
    except Exception as exc:  # adapter subprocess failure
        return OpResult(
            True,
            f"restored {pid}'s conversation to the archive as ghost-restored, but the "
            f"tmux spawn failed ({exc}) — the watchdog will revive it on its next "
            f"pass{kill_suffix}",
            # Neither a tmux session nor a tab: the conversation is preserved,
            # but nothing the user clicked for actually happened. Not gated on
            # tabs_expected — a missing tmux session matters on every host,
            # headless included, and this must not be the one outcome that
            # still reads as plain success now that lesser ones don't.
            degraded=True,
        )
    suffix, landed = _open_tab(tab_spawner, name)
    return OpResult(
        True, f"restored {pid}'s conversation into detached tmux as {name}{kill_suffix}"
        + suffix, degraded=tabs_expected and not landed
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
    name = entry.get("tmux_session")
    if not name:
        return OpResult(False, f"session {pid} is not tmux-parked")
    live = tmux.list_sessions()
    if live is None:
        return OpResult(False, f"detmux {pid}: cannot determine tmux state — is tmux responding?")
    if name not in live:
        return OpResult(False, f"tmux session {name} is gone")
    # PARKED, not CRASHED (#58). CRASHED was only ever standing in for
    # "parked in tmux" — before the journal was re-keyed onto the revived
    # claude, a parked entry's pid was always the dead shell. Now it is the
    # live pane process, so refusing on liveness would make this op unusable
    # on exactly the sessions it exists for.
    #
    # But liveness alone must not be dropped: [bug 2026-07-29] a live SHELL
    # that inherited this tmux_session via same-boot pid preservation must
    # still be refused, or it gets archived+delisted out of crr management.
    # The exact discriminator is whether the journaled pid IS the process
    # running in that session — true for a re-keyed parked entry, false for
    # a shell wearing an inherited name.
    if state != CRASHED and tmux.session_pid(name) != pid:
        return OpResult(False, f"session {pid} is {state}, not parked — refusing "
                               "(detmux re-homes revived sessions only)")
    if tab_spawner is None or not tab_spawner.available():
        return OpResult(False, "no terminal tab spawner available on this host")
    try:
        tab_spawner.open_tab(attach_argv(name))
    except Exception as exc:  # adapter subprocess/osascript failure
        return OpResult(False, f"detmux {pid} failed to open a tab: {exc}")
    if entry.get("claude") is not None:
        # Terminology: detmux -> untrack (dashboard/CLI); "detmuxed" stays a
        # valid archive reason for pre-rename records, but new archives use
        # the current name.
        archive.archive(entry, "untracked", now)
    store.remove(pid)
    return OpResult(True, f"de-tmuxed {pid}: attached {name} in a tab; crr no longer manages it")


def untmux(
    store: JournalStore,
    archive: ArchiveStore,
    tmux: TmuxSpawner,
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    now: str,
    *,
    remote_control: bool,
    tab_spawner: TabSpawner | None,
) -> OpResult:
    """Genuinely un-tmux a parked session: kill the tmux wrapper, relaunch
    ``claude --resume <sid>`` directly in a visible tab.

    The honest counterpart to ``detmux`` (which only re-homes into a tab
    that still runs tmux underneath — see its docstring). Same gates, same
    order: entry -> classify == CRASHED -> tmux_session set -> the named
    session is actually live -> a tab spawner is available. The spawner
    check runs BEFORE the kill deliberately: a missing spawner must refuse
    without touching the tmux session at all, exactly like ``detmux``.

    Ordering after the gates, each choice load-bearing:

    1. Kill the tmux session. A kill failure (e.g. the underlying
       ``tmux kill-session`` erroring) leaves the entry untouched and fails
       the op — nothing destructive has landed, so there is nothing to
       recover.
    2. Spawn the visible tab running ``claude --resume <sid>`` word-form
       with ``cwd=entry["cwd"]``.
    3. On spawn success: archive reason ``"untmuxed"`` (terminal — see
       ``reviver``'s skip tuple), delist the entry. The conversation is now
       a bare ``claude --resume`` in a tab; crr no longer manages it.
    4. On spawn failure AFTER the kill: the tmux session is already gone,
       so leave the journal entry untouched (its ``tmux_session`` field
       remains) — the next revive pass finds no live session under that
       name and re-parks the conversation in a fresh detached tmux
       session, exactly like any other crashed candidate. Say so in the
       failure message.
    """
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    state = classify(entry, boot, probe)
    name = entry.get("tmux_session")
    if not name:
        return OpResult(False, f"session {pid} is not tmux-parked")
    live = tmux.list_sessions()
    if live is None:
        return OpResult(False, f"untmux {pid}: cannot determine tmux state — is tmux responding?")
    if name not in live:
        return OpResult(False, f"tmux session {name} is gone")
    # PARKED, not CRASHED (#58). CRASHED was only ever standing in for
    # "parked in tmux" — before the journal was re-keyed onto the revived
    # claude, a parked entry's pid was always the dead shell. Now it is the
    # live pane process, so refusing on liveness would make this op unusable
    # on exactly the sessions it exists for.
    #
    # But liveness alone must not be dropped: [bug 2026-07-29] a live SHELL
    # that inherited this tmux_session via same-boot pid preservation must
    # still be refused, or it gets archived+delisted out of crr management.
    # The exact discriminator is whether the journaled pid IS the process
    # running in that session — true for a re-keyed parked entry, false for
    # a shell wearing an inherited name.
    if state != CRASHED and tmux.session_pid(name) != pid:
        return OpResult(False, f"session {pid} is {state}, not parked — refusing "
                               "(untmux re-homes revived sessions only)")
    if tab_spawner is None or not tab_spawner.available():
        return OpResult(False, "no terminal tab spawner available on this host")
    try:
        tmux.kill_session(name)
    except Exception as exc:  # adapter subprocess failure
        return OpResult(False, f"untmux {pid} failed to kill tmux session {name}: {exc}")
    try:
        tab_spawner.open_tab(
            revival_argv(entry, remote_control=remote_control), cwd=entry["cwd"]
        )
    except Exception as exc:  # adapter subprocess/osascript failure
        return OpResult(
            False,
            f"untmux {pid}: tmux session killed, but the tab failed to open: {exc}; "
            "the watchdog will re-park it in tmux within a minute",
        )
    if entry.get("claude") is not None:
        archive.archive(entry, "untmuxed", now)
    store.remove(pid)
    return OpResult(True, f"un-tmuxed {pid}: claude --resume in a new tab; crr no longer manages it")


def retrack(store: JournalStore, archive: ArchiveStore, sid: str, now: str) -> OpResult:
    """Undo untrack/detmux: restore an archived untracked session to the
    active journal.

    Reads the archive record for ``sid`` (a missing record, or one whose id
    fails the UUID shape check, refuses honestly rather than guessing —
    ``archive.read`` raises ``KeyError``/``ContractError`` for either). Only
    records left the active set via untrack/detmux (reason ``"untracked"``,
    or the deprecated ``"detmuxed"`` spelling for pre-rename records) are
    eligible: any other reason (e.g. ``"dismissed"``, ``"ghost-restored"``)
    means the entry left crr's management for a different reason, and
    resurrecting it under the wrong pretext would misrepresent why it's
    back. On success the preserved entry is re-journaled verbatim and the
    archive record removed — the mirror image of what untrack/detmux did.

    Recycled-pid guard (mirrors ``_adopt``'s and ``_cmd_register``'s
    collision checks in ``crr.cli``): the archived entry's pid slot may no
    longer be free by the time retrack runs — the OS can recycle a pid to
    an unrelated, currently-tracked session in the time between untrack and
    retrack. Blindly writing there would clobber that live entry, and then
    ``archive.remove`` would destroy the only remaining record of the
    conversation being retracked — a double loss. So the pid slot is
    checked BEFORE either destructive step:
    - empty (``store.read`` raises ``KeyError``) -> proceed.
    - occupied by an entry whose claude session_id is this same sid ->
      already tracked -> refuse (the archive record is left intact; this
      is not the "restore" it looks like, so there's nothing to preserve).
    - occupied by a different session -> refuse; that entry is a bystander
      and must not be overwritten, and the archive record must survive so
      the conversation isn't lost.
    - unreadable (``ContractError``/``OSError``) -> refuse rather than
      guess whose slot it is.
    """
    try:
        record = archive.read(sid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no archived session {sid}")
    if record["reason"] not in ("untracked", "detmuxed"):
        return OpResult(
            False,
            f"session {sid} was archived as {record['reason']!r}, not untracked — refusing",
        )
    entry = dict(record["entry"])
    entry["updated"] = now  # re-journaling is itself a change; a stale timestamp would lie
    pid = entry["pid"]
    try:
        existing = store.read(pid)
    except KeyError:
        existing = None  # slot is genuinely empty — safe to write
    except (contracts.ContractError, OSError):
        return OpResult(
            False, f"cannot retrack {sid[:8]}: pid slot {pid} is unreadable, refusing to guess"
        )
    if existing is not None:
        if (existing.get("claude") or {}).get("session_id") == sid:
            return OpResult(False, f"session {sid[:8]} is already tracked")
        return OpResult(
            False,
            f"cannot retrack {sid[:8]}: pid slot {pid} now belongs to a different session",
        )
    store.write(entry)
    archive.remove(sid)
    return OpResult(True, f"retracked {sid[:8]}")


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


def _open_tab(tab_spawner: TabSpawner | None, name: str) -> tuple[str, bool]:
    """Open a visible tab attaching to ``name``; return (suffix, landed).

    The tmux revival is already durable by the time this runs, so a failure
    here is never fatal to the session — but it is not nothing either: the
    caller asked for a tab. ``landed`` lets the caller mark the op degraded
    instead of silently reporting success ([user request, 2026-08-09]).
    """
    if tab_spawner is None or not tab_spawner.available():
        # Honesty: revival already landed (durable) — say why no tab
        # appeared instead of looking like the whole op did nothing, and
        # give the manual fallback ([live bug, 2026-07-31]).
        return f" (no tab spawner on this host — attach with: tmux attach -t {name})", False
    try:
        tab_spawner.open_tab(attach_argv(name))
        return " (opened in a new tab)", True
    except TabSpawnTimeout as exc:
        # Degraded, but honestly: we do not know that no tab opened, and
        # sending the user to `tmux attach` for a tab that is already on its
        # way would be its own kind of wrong (#53).
        return (
            f" (no tab confirmed within {exc.seconds:g}s — the terminal may still be "
            f"starting; if none appears, attach with: tmux attach -t {name})",
            False,
        )
    except Exception as exc:  # best-effort: an osascript/subprocess failure
        # Same honesty as the no-spawner branch: the revival is durable and
        # the session is attachable, so name the cause AND the way in. A bare
        # errno reads as "the whole op failed" ([live bug, 2026-08-09]: WSL
        # ENOEXEC on wt.exe when the WSLInterop binfmt handler is missing).
        return f" (tab spawn failed: {exc} — attach with: tmux attach -t {name})", False
