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

import re
from typing import Any, Mapping, NamedTuple, Sequence

from crr.core.archive import ArchiveStore
from crr.core.classifier import CRASHED, classify
from crr.core.journal import JournalStore
from crr.core.ports import BootIdentity, ProcessProbe, TmuxSpawner

# Remote Control session names: letters, digits, dash, underscore only.
# Runs of anything else collapse to a single dash so an odd basename (a
# path with spaces, parens, unicode punctuation, ...) still yields one
# clean token rather than a run of separators.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
# ~40 chars: plenty to identify a project directory in the mobile list,
# short enough that it can't be mistaken for the rest of the command line.
_MAX_NAME_LEN = 40


class RevivalOutcome(NamedTuple):
    revived: list[int]   # pids (re)spawned into tmux this pass
    gave_up: list[int]   # pids abandoned to the archive past the strike limit
    reset: list[int]     # pids whose live session cleared their strikes
    skipped: bool = False  # True when the whole pass was skipped (tmux liveness unknown)


def session_name(entry: Mapping[str, Any]) -> str:
    """Deterministic tmux session name for an entry's claude session.

    The WHOLE session id, not the ``sid8`` short form. ``sid8`` is a display
    abbreviation (payload contract, dashboard cards); this name is an
    identity — crr decides a conversation is already parked by matching it
    against live tmux sessions. Two session ids sharing their first 8
    characters produced the same name, and Reopen then attached the user to
    the *other* conversation while reporting success (#51). A wider prefix
    would only lower the odds; the full id removes them.

    Still metacharacter-free (hex and dashes), and tmux resolves ``-t`` by
    prefix, so ``tmux attach -t crr-79e5`` keeps working.
    """
    return f"crr-{entry['claude']['session_id']}"


def resolved_session_name(entry: Mapping[str, Any]) -> str:
    """The tmux session name to use for ``entry`` — recorded name wins.

    A conversation already parked under the legacy ``crr-<sid8>`` must keep
    answering to that name. Recomputing it would leave the live session
    unmatched, and both ``reviver._decide`` and ``ops.reopen`` read "no live
    session" as "revive it" — spawning a SECOND ``claude --resume`` on a
    conversation that already has one (the hazard in #48). Preferring the
    recorded name is what makes the rename safe with no migration step.
    """
    return entry.get("tmux_session") or session_name(entry)


def remote_control_name(cwd: str) -> str:
    """A safe Claude Code Remote Control session name derived from a
    session's working directory (its basename), so the mobile session list
    shows a meaningful name instead of an auto-generated one.

    Sanitized to letters/digits/dash/underscore (the claude CLI's session
    name has no documented character restrictions, but a shell-word-safe,
    metacharacter-free token is what makes it safe to interpolate into a
    shim's argv without quoting gymnastics — same rationale as the
    ``crr-<8hex>`` tmux session names elsewhere in this module). An empty,
    root, or entirely-punctuation cwd falls back to ``"crr"`` rather than
    an empty name (Remote Control's own auto-naming would kick in for an
    empty value, defeating the point of naming it at all).
    """
    trimmed = cwd.rstrip("/")
    base = trimmed.rsplit("/", 1)[-1] if trimmed else ""
    token = _UNSAFE_NAME_CHARS.sub("-", base).strip("-")
    return token[:_MAX_NAME_LEN].rstrip("-") or "crr"


def remote_control_flag_argv(cwd: str) -> list[str]:
    """The ``--remote-control <name>`` pair for a session's cwd.

    Always an explicit name, never a bare ``--remote-control``: the flag's
    value is OPTIONAL on the claude CLI, so anything that followed an
    unnamed flag on the command line risks being swallowed as the session
    name (e.g. ``--remote-control --resume <sid>`` reading ``--resume`` as
    the name). An explicit name is unambiguous regardless of what follows
    it — callers are free to place this pair anywhere in their argv.
    """
    return ["--remote-control", remote_control_name(cwd)]


def revival_argv(entry: Mapping[str, Any], *, remote_control: bool) -> list[str]:
    """Word-form argv to resume the session (never a shell string).

    ``remote_control`` is injected rather than read from config here (core
    stays pure) — the caller (the CLI's ``revive`` command) resolves it
    from the ``remote_control`` config key. Required, not defaulted: a
    forgotten wire-up at the call site should fail loudly, not silently
    revive sessions unreachable from the phone.
    """
    argv = ["claude", "--resume", entry["claude"]["session_id"]]
    if remote_control:
        # Appended last so nothing on the line can follow (and be swallowed
        # by) the name — belt-and-suspenders on top of the explicit name
        # remote_control_flag_argv already guarantees.
        argv += remote_control_flag_argv(entry["cwd"])
    return argv


def attach_argv(name: str) -> list[str]:
    """Word-form argv for a visible tab to attach to the detached session.

    The session name is ``crr-<session id>`` (metacharacter-free), so this stays
    safe even where an adapter must render it into a shell string.
    """
    return ["tmux", "attach", "-t", name]


def _decide(entry: Mapping[str, Any], live: set[str], max_strikes: int, now: str):
    """Return (action, updated_entry, name) for one candidate.

    action is one of: 'reset-nochange', 'reset', 'revive', 'give_up'.
    """
    name = resolved_session_name(entry)  # legacy crr-<sid8> keeps its name (#51)
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
    remote_control_enabled: bool,
) -> RevivalOutcome:
    live = tmux.list_sessions()
    if live is None:
        # F16 tri-state: an unknown tmux liveness must never be treated as
        # "confirmed dead" — that would accumulate a strike (or trigger a
        # give-up archive) against a session that may in fact still be
        # alive. Skip the entire pass rather than guess. The `skipped` flag
        # is what actually makes "can't tell" distinguishable from "nothing
        # to do" for a caller — the three empty lists alone read identical
        # in both cases, so this pass has to say so explicitly.
        return RevivalOutcome([], [], [], skipped=True)
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
            tmux.new_detached_session(
                name, entry["cwd"], revival_argv(entry, remote_control=remote_control_enabled)
            )
            live.add(name)  # dedupe within the pass: a shared sid is now "live"
            store.write(updated)
            revived.append(pid)

    # 2. Archived records awaiting revival (skip the terminal ones: 'gave-up'
    #    is abandoned for good, 'untracked' (formerly 'detmuxed' — terminology
    #    change: detmux -> untrack; both spellings are terminal here, the old
    #    one kept for pre-rename records) has been re-homed to a visible
    #    tab under the user's manual ownership — reviving it here would
    #    resurrect the conversation the moment the user exits claude in that
    #    tab, exactly what untrack's delist is meant to prevent — 'untmuxed'
    #    is the same terminality one step further (the tab runs a bare
    #    `claude --resume`, no tmux wrapper left at all — reviving it would
    #    both resurrect the conversation and contradict the tmux session
    #    ops.untmux deliberately killed) — and 'dismissed' is the user's
    #    explicit "clean up without restoring"; reviving it would un-dismiss
    #    their decision. The two 'superseded-*' reasons stay revivable on
    #    purpose: their archives exist to preserve revival data.)
    for record in archive.scan().records:
        if record["reason"] in ("gave-up", "detmuxed", "untracked", "untmuxed", "dismissed"):
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
            tmux.new_detached_session(
                name, entry["cwd"], revival_argv(entry, remote_control=remote_control_enabled)
            )
            live.add(name)  # dedupe within the pass (shared sid)
            record["entry"] = updated
            archive.write(record)
            revived.append(pid)

    return RevivalOutcome(revived, gave_up, reset)
