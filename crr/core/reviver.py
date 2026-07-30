"""Reviver — bring crashed claude sessions back as detached tmux sessions.

Two sources of revival candidates:

- **Active journal entries** that classify ``crashed`` and still carry a
  claude session id (the shell died mid-session — claude-exit never ran).
- **Archived records** (reasons ``superseded-on-register``, ``superseded-on-launch``, ``ghost-restored``): sessions
  preserved when a reboot/pid-reuse would otherwise have clobbered their
  revival data. Reviving from the archive is what makes reboot recovery
  survive pid reuse — the data lives under the session id, not the pid.

For every candidate the same rule applies, gated on a LIVE session check
(``tmux.list_sessions()``), not the persisted ``tmux_session`` field: a
reboot leaves a fresh tmux server with no sessions, so a field-based gate
would refuse to revive the very sessions the reviver exists for.

The give-up guard is the safety valve for that gate. Each revival
increments a strike; past ``max_strikes`` the session is abandoned to the
archive with reason ``gave-up`` (its terminal home — it stops being a
candidate and stops re-reporting). Observing the session alive resets
strikes to zero, so only *persistent* failures accumulate.

Pure core: takes the ports + the journal and archive stores, so it is
fully testable with fakes and touches no OS directly.
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

from crr.core.archive import ArchiveStore
from crr.core.classifier import CRASHED, classify
from crr.core.journal import JournalStore
from crr.core.ports import BootIdentity, ProcessProbe, TmuxSpawner


class RevivalOutcome(NamedTuple):
    revived: list[int]   # pids (re)spawned into tmux this pass
    gave_up: list[int]   # pids abandoned to the archive past the strike limit
    reset: list[int]     # pids whose live session cleared their strikes


def session_name(entry: Mapping[str, Any]) -> str:
    """Deterministic tmux session name for an entry's claude session."""
    return f"crr-{entry['claude']['session_id'][:8]}"


def revival_argv(entry: Mapping[str, Any]) -> list[str]:
    """Word-form argv to resume the session (never a shell string)."""
    return ["claude", "--resume", entry["claude"]["session_id"]]


def attach_argv(name: str) -> list[str]:
    """Word-form argv for a visible tab to attach to the detached session.

    The session name is ``crr-<8hex>`` (metacharacter-free), so this stays
    safe even where an adapter must render it into a shell string.
    """
    return ["tmux", "attach", "-t", name]


def _decide(entry: Mapping[str, Any], live: set[str], max_strikes: int, now: str):
    """Return (action, updated_entry, name) for one candidate.

    action is one of: 'reset-nochange', 'reset', 'revive', 'give_up'.
    """
    name = session_name(entry)
    if name in live:
        if entry["revive_strikes"] == 0 and entry["tmux_session"] == name:
            return "reset-nochange", entry, name
        updated = dict(entry)
        updated["tmux_session"] = name
        updated["revive_strikes"] = 0
        updated["updated"] = now
        return "reset", updated, name
    if entry["revive_strikes"] >= max_strikes:
        return "give_up", entry, name
    updated = dict(entry)
    updated["tmux_session"] = name
    updated["revive_strikes"] = entry["revive_strikes"] + 1
    updated["updated"] = now
    return "revive", updated, name


def revive_crashed(
    entries: Sequence[Mapping[str, Any]],
    boot_identity: BootIdentity,
    process_probe: ProcessProbe,
    tmux: TmuxSpawner,
    store: JournalStore,
    archive: ArchiveStore,
    *,
    max_strikes: int,
    now: str,
) -> RevivalOutcome:
    live = tmux.list_sessions()
    revived: list[int] = []
    gave_up: list[int] = []
    reset: list[int] = []

    # 1. Active crashed-with-claude entries.
    for entry in entries:
        if entry.get("claude") is None:
            continue
        if classify(entry, boot_identity, process_probe) != CRASHED:
            continue
        action, updated, name = _decide(entry, live, max_strikes, now)
        pid = entry["pid"]
        if action == "reset-nochange":
            reset.append(pid)
        elif action == "reset":
            store.write(updated)
            reset.append(pid)
        elif action == "give_up":
            # Terminal home: preserve in the archive, drop from active.
            archive.archive(entry, "gave-up", now)
            store.remove(pid)
            gave_up.append(pid)
        else:  # revive
            tmux.new_detached_session(name, entry["cwd"], revival_argv(entry))
            live.add(name)  # dedupe within the pass: a shared sid is now "live"
            store.write(updated)
            revived.append(pid)

    # 2. Archived records awaiting revival (skip the terminal ones: 'gave-up'
    #    is abandoned for good, 'detmuxed' has been re-homed to a visible
    #    tab under the user's manual ownership — reviving it here would
    #    resurrect the conversation the moment the user exits claude in that
    #    tab, exactly what detmux's delist is meant to prevent — and
    #    'dismissed' is the user's explicit "clean up without restoring";
    #    reviving it would un-dismiss their decision. The two 'superseded-*'
    #    reasons stay revivable on purpose: their archives exist to preserve
    #    revival data.)
    for record in archive.scan().records:
        if record["reason"] in ("gave-up", "detmuxed", "dismissed"):
            continue
        entry = record["entry"]
        action, updated, name = _decide(entry, live, max_strikes, now)
        pid = entry["pid"]
        if action == "reset-nochange":
            reset.append(pid)
        elif action == "reset":
            record["entry"] = updated
            archive.write(record)
            reset.append(pid)
        elif action == "give_up":
            record["reason"] = "gave-up"
            archive.write(record)
            gave_up.append(pid)
        else:  # revive
            tmux.new_detached_session(name, entry["cwd"], revival_argv(entry))
            live.add(name)  # dedupe within the pass (shared sid)
            record["entry"] = updated
            archive.write(record)
            revived.append(pid)

    return RevivalOutcome(revived, gave_up, reset)
