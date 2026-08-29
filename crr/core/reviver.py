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
import shlex
from typing import Any, Mapping, NamedTuple, Sequence

from crr.core import tab_health as tab_health_module
from crr.core.archive import ArchiveStore
from crr.core.classifier import CRASHED, classify
from crr.core.journal import JournalStore
from crr.core.ports import BootIdentity, ProcessProbe, TabSpawnTimeout, TmuxSpawner

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
    if entry["claude"].get("skip_permissions", False):
        argv.append("--dangerously-skip-permissions")
    if remote_control:
        # Appended last so nothing on the line can follow (and be swallowed
        # by) the name — belt-and-suspenders on top of the explicit name
        # remote_control_flag_argv already guarantees.
        argv += remote_control_flag_argv(entry["cwd"])
    return argv


def exit_hook_argv(claude_argv: Sequence[str], crr_bin: str) -> list[str]:
    """Wrap a bare claude launch so a CLEAN (exit 0) exit deregisters it.

    A tmux-revived claude runs with no shell between it and tmux (#58), so
    when the user attaches and ``/exit``s, nothing clears the journal: the
    dead pane pid classifies CRASHED and the very next revive pass
    resurrects the conversation the user deliberately ended (the
    tmux-parked twin of the shim's ``claude-exit`` gap). Interpose a
    minimal ``sh`` that, on claude's clean exit, runs ``crr deregister
    --reason closed`` — archiving terminally and delisting, exactly the
    bookkeeping the shim's ``fish_exit`` does for a hosted session. A
    non-zero exit (a genuine crash) runs nothing, so real crashes still
    revive.

    claude's own arguments ride through ``"$@"`` — never interpolated into
    the script text — so a session id, cwd, or remote-control name cannot
    smuggle a shell metacharacter into the command; only ``crr_bin`` (a
    trusted, crr-resolved absolute path) and the fixed ``claude`` command
    word are embedded, both ``shlex.quote``-escaped. ``$$`` is the pane
    ``sh``'s pid — exactly the pid the reviver re-keys the journal onto
    (``_rekey_onto_live_pid`` reads ``#{pane_pid}``, which is this ``sh``,
    not its claude child) — so ``deregister --pid $$`` targets the right
    slot. Because the pane process is now a shell hosting a claude child
    (not claude itself), a revived session finally has the SAME shape as a
    shim-hosted one, which is what lets ``kick`` find and signal it (see
    ``_child_groups``' ``include_shell_group``).
    """
    prog = claude_argv[0]
    script = (
        f'{shlex.quote(prog)} "$@"; '
        f'test $? -eq 0 && {shlex.quote(crr_bin)} deregister --pid $$ --reason closed'
    )
    return ["sh", "-c", script, "sh", *claude_argv[1:]]


def attach_argv(name: str) -> list[str]:
    """Word-form argv for a visible tab to attach to the detached session.

    The session name is ``crr-<session id>`` (metacharacter-free), so this stays
    safe even where an adapter must render it into a shell string.
    """
    return ["tmux", "attach", "-t", name]


def _try_open_tab(tab_spawner, name: str, tab_health=None, *,
                   now: str = "", boot_id: str = "") -> bool:
    """Best-effort visible tab attaching to ``name``. Never raises.

    Deliberately local rather than reusing ``ops._open_tab``: ``ops``
    imports this module, so importing it back would be a cycle. This one
    also has no message to build — the reviver runs unattended, so a tab
    that does not appear is not something a human is waiting to read about.

    ``tab_health`` records which launcher tier ``tab_spawner`` reports it
    used (spec 2026-08-29, Task 3), on the success path and on a genuine
    (non-timeout) failure — never on a ``TabSpawnTimeout``, whose fate is
    unknown (#53): recording one would be indistinguishable from a
    confirmed success.
    """
    try:
        if not tab_spawner.available():
            return False
        tab_spawner.open_tab(attach_argv(name))
        tab_health_module.record_from_spawner(tab_health, tab_spawner, now=now, boot_id=boot_id)
        return True
    except TabSpawnTimeout:
        return False  # unknown fate — never record, never treat as failure
    except Exception:
        tab_health_module.record_from_spawner(tab_health, tab_spawner, now=now, boot_id=boot_id)
        return False  # the revival is already durable; the tab is not


def _rekey_onto_live_pid(store, tmux, boot_identity, entry, name, now) -> bool:
    """Re-key ``entry`` onto the pid actually running in tmux session ``name``.

    The reviver spawns claude under a minimal ``sh`` exit-hook wrapper ([/exit revival 2026-08-24];
    ``exit_hook_argv``), never the interactive shim, so the revived claude
    never runs the shim's ``claude()`` wrapper and never calls ``crr
    register``. Every revived conversation was therefore absent from the
    journal: the only entry stayed keyed to the long-dead shell pid, so its
    card read "crashed" and offered Reopen instead of Kick while the
    conversation was alive (#58). Re-keying targets the pane pid (the ``sh``
    wrapper), which hosts claude as its child — the shape kick expects.

    The current boot id is stamped along with the pid — ``classify`` returns
    CRASHED on a boot mismatch *without consulting the pid at all*, so a
    re-keyed entry carrying the old boot would stay crashed no matter how
    alive its process is. The revived process genuinely belongs to this boot.

    Returns True if the entry moved. Declines (leaving everything untouched)
    when the pane pid is unknown — never guess a pid to point every pid-keyed
    op at — or when that slot already belongs to a different conversation,
    the same refuse-rather-than-clobber discipline adopt/retrack use.
    """
    live_pid = tmux.session_pid(name)
    if live_pid is None or live_pid == entry["pid"]:
        return False
    try:
        existing = store.read(live_pid)
    except KeyError:
        existing = None
    except Exception:
        return False  # unreadable slot: refuse rather than guess
    if existing is not None:
        same = (existing.get("claude") or {}).get("session_id") == \
            (entry.get("claude") or {}).get("session_id")
        if not same:
            return False
    moved = dict(entry)
    moved["pid"] = live_pid
    moved["boot_id"] = boot_identity.current()
    moved["tmux_session"] = name
    moved["host"] = "tmux"
    moved["updated"] = now
    store.write(moved)
    store.remove(entry["pid"])
    return True


def _journal_from_archive(store, tmux, boot_identity, entry, name, now) -> bool:
    """Re-journal an archive entry keyed on its live tmux pid.

    Unlike ``_rekey_onto_live_pid`` (which assumes ``entry["pid"]`` is the
    entry's own journal slot), an archive entry's pid may be occupied by a
    different live session — the exact superseded-on-register case, where
    the new shell reused the old pid.  Writing there would clobber it.

    Writes directly at the live pane pid when known, falling back to
    ``entry["pid"]`` only when that slot is genuinely free or same-session.
    Returns True if the entry was journaled.
    """
    live_pid = tmux.session_pid(name)
    if live_pid is not None and live_pid != entry["pid"]:
        try:
            existing = store.read(live_pid)
        except KeyError:
            existing = None
        except Exception:
            return False
        if existing is not None:
            same = (existing.get("claude") or {}).get("session_id") == \
                (entry.get("claude") or {}).get("session_id")
            if not same:
                return False
        moved = dict(entry)
        moved["pid"] = live_pid
        moved["boot_id"] = boot_identity.current()
        moved["tmux_session"] = name
        moved["host"] = "tmux"
        moved["updated"] = now
        store.write(moved)
        return True

    try:
        existing = store.read(entry["pid"])
    except KeyError:
        existing = None
    except Exception:
        return False
    if existing is not None:
        same = (existing.get("claude") or {}).get("session_id") == \
            (entry.get("claude") or {}).get("session_id")
        if not same:
            return False
    store.write(dict(entry))
    return True


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
    flags=None,
    tab_spawner=None,
    crr_bin: str | None = None,
    tab_health=None,
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

    def _spawn_argv(e: Mapping[str, Any]) -> list[str]:
        # crr_bin is injected (composition root resolves it, like
        # remote_control) so core stays pure. When present, wrap the launch
        # so a clean /exit deregisters itself [/exit revival 2026-08-24]; when absent (older
        # callers, tests that don't exercise the hook) fall back to the bare
        # command — the pre-fix behaviour, which degrades safely to "revive
        # again" rather than emitting a broken script.
        argv = revival_argv(e, remote_control=remote_control_enabled)
        return exit_hook_argv(argv, crr_bin) if crr_bin else argv

    # 1. Active crashed-with-claude entries.
    for entry in entries:
        if entry.get("claude") is None:
            continue
        if classify(entry, boot_identity, process_probe) != CRASHED:
            continue
        # `close` arms a flag the SHIM's repair loop consumes; the shim then
        # deregisters, and THAT is what stops this sweep. A tmux-revived
        # claude has no shim (#58), so nothing consumed the flag and the
        # next pass revived the very conversation the user just closed.
        # Honour it here: archive terminally, delist, and clear the flag so
        # it cannot linger onto a recycled pid.
        #
        # A stale flag from a previous boot (recycled pid) must NOT be
        # honored — it would archive the wrong session (#98). Clear it and
        # fall through to normal revival. Legacy flags (no boot_id) are
        # honored to avoid breaking pre-upgrade state.
        if flags is not None:
            armed = flags.read(entry["pid"])
            if armed is not None and armed[0] == "close":
                flag_boot = armed[2] if len(armed) > 2 else None
                current_boot = boot_identity.current()
                if flag_boot is not None and flag_boot != current_boot:
                    flags.clear(entry["pid"])
                else:
                    archive.archive(entry, "closed", now)
                    store.remove(entry["pid"])
                    flags.clear(entry["pid"])
                    continue
        action, updated, name = _decide(entry, live, max_strikes, now)
        pid = entry["pid"]
        if action == "reset-nochange":
            # Already parked and healthy — but possibly never journaled under
            # its live pid (every session revived before #58 is in this
            # state). Adopt it now rather than waiting for a re-revival.
            _rekey_onto_live_pid(store, tmux, boot_identity, entry, name, now)
            reset.append(pid)
        elif action == "reset":
            store.write(updated)
            _rekey_onto_live_pid(store, tmux, boot_identity, updated, name, now)
            reset.append(pid)
        elif action == "give_up":
            # Terminal home: preserve in the archive, drop from active.
            archive.archive(entry, "gave-up", now)
            store.remove(pid)
            gave_up.append(pid)
        else:  # revive
            tmux.new_detached_session(name, entry["cwd"], _spawn_argv(entry))
            live.add(name)  # dedupe within the pass: a shared sid is now "live"
            store.write(updated)
            # The spawn just created the process crr must be able to Kick;
            # put the journal on it before the pass ends (#58).
            _rekey_onto_live_pid(store, tmux, boot_identity, updated, name, now)
            # An armed `relaunch` flag means a human pressed Kick and the
            # shim never consumed it — i.e. a tmux-parked session with no
            # shim (#58). That is the one revival that was ASKED for, so it
            # is the one that gets a tab: Kick means "restart it and put it
            # in front of me" (#62). A crash-driven revival carries no flag
            # and stays tabless — 13 of those fire at boot on the reporting
            # host. Best-effort: the revival is already durable, so a
            # spawner failure costs the tab, never the conversation.
            if flags is not None and tab_spawner is not None:
                armed = flags.read(pid)
                if armed is not None and armed[0] == "relaunch":
                    flag_boot = armed[2] if len(armed) > 2 else None
                    if flag_boot is not None and flag_boot != boot_identity.current():
                        flags.clear(pid)
                    else:
                        _try_open_tab(tab_spawner, name, tab_health,
                                      now=now, boot_id=boot_identity.current())
                        flags.clear(pid)
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
        if record["reason"] in ("gave-up", "detmuxed", "untracked", "untmuxed",
                                "dismissed", "closed"):
            continue
        entry = record["entry"]
        action, updated, name = _decide(entry, live, max_strikes, now)
        pid = entry["pid"]
        sid = (entry.get("claude") or {}).get("session_id")
        if action == "reset-nochange":
            if _journal_from_archive(store, tmux, boot_identity, entry, name, now):
                if sid:
                    archive.remove(sid)
            reset.append(pid)
        elif action == "reset":
            if _journal_from_archive(store, tmux, boot_identity, updated, name, now):
                if sid:
                    archive.remove(sid)
            reset.append(pid)
        elif action == "give_up":
            record["reason"] = "gave-up"
            archive.write(record)
            gave_up.append(pid)
        else:  # revive
            tmux.new_detached_session(name, entry["cwd"], _spawn_argv(entry))
            live.add(name)  # dedupe within the pass (shared sid)
            if _journal_from_archive(store, tmux, boot_identity, updated, name, now):
                if sid:
                    archive.remove(sid)
            revived.append(pid)

    return RevivalOutcome(revived, gave_up, reset)
