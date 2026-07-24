"""Reviver — bring crashed claude sessions back as detached tmux sessions.

A revival candidate is a journal entry that classifies ``crashed``, still
carries a claude session id (the shell died mid-session — claude-exit
never ran), and has **no live tmux session** of its deterministic name.

Gating on a live-session check (not on the persisted ``tmux_session``
field) is what makes reboot recovery work: after a reboot the tmux server
and its sessions are gone but the field still points at the old name, so a
field-based gate would refuse to revive the very sessions the reviver
exists for.

The give-up guard is the safety valve for that gate. Without it, a claude
that dies immediately on ``--resume`` would be re-revived every watchdog
cycle forever ([lesson: give-up guard]). So each revival increments a
strike; past ``max_strikes`` the session is abandoned. Observing the
session alive resets strikes to zero, so only *persistent* failures
accumulate — a session that revives fine never creeps toward give-up.

Pure core: takes the TmuxSpawner/BootIdentity/ProcessProbe ports and the
JournalStore, so it is fully testable with fakes and touches no OS
directly.
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

from crr.core.classifier import CRASHED, classify
from crr.core.journal import JournalStore
from crr.core.ports import BootIdentity, ProcessProbe, TmuxSpawner


class RevivalOutcome(NamedTuple):
    revived: list[int]   # pids (re)spawned into tmux this pass
    gave_up: list[int]   # pids past the strike limit — abandoned, not respawned
    reset: list[int]     # pids whose live session cleared their strikes


def session_name(entry: Mapping[str, Any]) -> str:
    """Deterministic tmux session name for an entry's claude session."""
    return f"crr-{entry['claude']['session_id'][:8]}"


def revival_argv(entry: Mapping[str, Any]) -> list[str]:
    """Word-form argv to resume the session (never a shell string)."""
    return ["claude", "--resume", entry["claude"]["session_id"]]


def revive_crashed(
    entries: Sequence[Mapping[str, Any]],
    boot_identity: BootIdentity,
    process_probe: ProcessProbe,
    tmux: TmuxSpawner,
    store: JournalStore,
    *,
    max_strikes: int,
    now: str,
) -> RevivalOutcome:
    live = tmux.list_sessions()
    revived: list[int] = []
    gave_up: list[int] = []
    reset: list[int] = []

    for entry in entries:
        if entry.get("claude") is None:
            continue
        if classify(entry, boot_identity, process_probe) != CRASHED:
            continue

        name = session_name(entry)
        pid = entry["pid"]

        if name in live:
            # Revived and running — clear strikes so only persistent
            # failures accumulate. Write only if something actually changed.
            if entry["revive_strikes"] != 0 or entry["tmux_session"] != name:
                entry = dict(entry)
                entry["revive_strikes"] = 0
                entry["tmux_session"] = name
                entry["updated"] = now
                store.write(entry)
            reset.append(pid)
            continue

        if entry["revive_strikes"] >= max_strikes:
            gave_up.append(pid)
            continue

        tmux.new_detached_session(name, entry["cwd"], revival_argv(entry))
        entry = dict(entry)
        entry["tmux_session"] = name
        entry["revive_strikes"] += 1
        entry["updated"] = now
        store.write(entry)
        revived.append(pid)

    return RevivalOutcome(revived, gave_up, reset)
