"""Status assembler — journal entries -> /api/sessions payload (contract v17).

Pure core: takes already-scanned entries plus the BootIdentity and
ProcessProbe ports, classifies each entry, and emits the versioned
sessions payload. ``sid_source`` is copied onto every card so the
dashboard can weight ``guessed`` claims when it renders duplicate groups
(audit P3 — confidence + provenance travel with the data).

Duplicate detection here is deliberately simple and honest: entries that
journal the *same* ``session_id`` form a group. The refinement of
down-weighting a ``guessed`` claim against an ``injected`` one is the
dashboard's job — the raw provenance it needs is on each card.

``last_prompt``, ``model``, ``last_active``, and ``transcript_bytes`` all
come from one injected ``tail_facts`` extractor that reads them in a
single backward pass over the transcript (all live near the tail). The
default returns empty/zero values — an honest "not extracted", never a
fabricated line, model, or timestamp. ``last_active`` and
``transcript_bytes`` feed ``context_pressure`` (F2 — compaction badge),
computed here via ``crr.core.context_pressure.pressure``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Mapping, Sequence

from crr.core import contracts
from crr.core import reachability as _reachability
from crr.core import settings as _settings
from crr.core.config import DEFAULTS
from crr.core.classifier import CRASHED, classify, LIVE
from crr.core.context_pressure import pressure as _pressure
from crr.core.discovery import ADOPTED_BOOT_ID
from crr.core.ports import BootIdentity, ProcessProbe

# Display-only state (spec 2026-08-09, Phase 0). NOT a classifier state:
# `classify()` answers "may I act on this pid", and CRASHED is the right
# answer for a parked session — `ops.detmux`/`ops.untmux` guard on exactly
# that and re-home only crashed entries. This projection changes what the
# CARD says, and nothing else.
PARKED = "parked"


def _empty_facts(_entry: Mapping[str, Any]) -> dict[str, Any]:
    # Mirrors `transcript_source.read_tail_facts`'s shape exactly, so the
    # default and the real adapter cannot drift. No bridge keys: the card's
    # `remote_control` comes from `reachability_by_sid` (Claude Code's own
    # state file), never from a transcript read.
    return {"last_prompt": "", "model": "", "last_active": "",
            "last_reply": "", "title": "", "slug": "", "transcript_bytes": 0}


class _MemoTtyProbe:
    """A ProcessProbe view with the tty check answered from a precomputed set.

    Wraps the injected probe so ``classify`` keeps calling
    ``has_controlling_tty`` per entry, but the answer is an O(1) membership
    test against one batched query — collapsing N ``ps`` spawns to one on the
    poll path. ``is_alive`` still delegates to the real probe (cheap
    ``os.kill``). Wrapping an injected port keeps this pure core (no adapter
    import).
    """

    def __init__(self, probe: ProcessProbe, tty_pids: set[int]) -> None:
        self._probe = probe
        self._tty_pids = tty_pids

    def is_alive(self, pid: int) -> bool:
        return self._probe.is_alive(pid)

    def has_controlling_tty(self, pid: int) -> bool:
        return pid in self._tty_pids


def owners_of_sid(sessions, owners, sid: str) -> list[int]:
    """Journaled pids that currently own a live claude for ``sid`` (#48).

    The pid-level counterpart to ``_conflicting_sids``: the shim needs to
    name the process a user would be choosing to end, not merely learn that
    a conflict exists.
    """
    return sorted(
        entry["pid"] for entry in sessions
        if (entry.get("claude") or {}).get("session_id") == sid
        and owners.get(entry["pid"])
    )


def _conflicting_sids(sessions, owners) -> set[str]:
    """Sids whose conversation has MORE THAN ONE live claude behind it (#48).

    `duplicate_group` cannot answer this: it fires whenever two entries
    share a sid, and the common case is benign — the shell that originally
    launched the conversation lingers in the journal beside the claude the
    reviver later parked in tmux, with only one agent running. What matters
    is which entries still OWN a claude process.
    """
    counts: dict[str, int] = {}
    for entry in sessions:
        if owners.get(entry["pid"]):
            sid = entry["claude"]["session_id"]
            counts[sid] = counts.get(sid, 0) + 1
    return {sid for sid, n in counts.items() if n > 1}


def _display_state(entry, boot_identity, probe, live_tmux_sessions) -> str:
    """The card's state: the operational state, except that an entry sitting
    in a confirmed-live tmux session reads PARKED.

    CRASHED covers entries not yet re-keyed. LIVE covers the post-#58 shape:
    a revived conversation is journaled onto the claude running in the pane,
    and a tmux pane HAS a controlling tty, so it classifies LIVE — without
    this it would read as a plain `live` card and lose the "this is in tmux,
    not a terminal you own" signal entirely.

    Still one-directional and still narrow: GHOST is never rewritten, and
    the LIVE case additionally requires ``host == "tmux"``, so a session the
    user is running in their own terminal can never be projected into
    `parked` just because some tmux session shares its name.
    """
    state = classify(entry, boot_identity, probe)
    if not live_tmux_sessions:
        return state
    if state not in (CRASHED, LIVE):
        return state
    if state == LIVE and entry.get("host") != "tmux":
        return state
    name = entry.get("tmux_session")
    return PARKED if name and name in live_tmux_sessions else state


def assemble_sessions(
    entries: Sequence[Mapping[str, Any]],
    boot_identity: BootIdentity,
    process_probe: ProcessProbe,
    *,
    tail_facts: Callable[[Mapping[str, Any]], dict[str, Any]] = _empty_facts,
    # (#37) These four were literal copies of config.DEFAULTS — the exact
    # shape run 2b fixed for web_restart_seconds/model_tail_lines, and the
    # exact way a default silently drifts from the config that documents it.
    # Sourced, not repeated. `config` is core, so this import crosses no
    # layer boundary.
    context_tight_fraction: float = DEFAULTS["context_tight_fraction"],
    context_compact_fraction: float = DEFAULTS["context_compact_fraction"],
    context_bytes_per_token: int = DEFAULTS["context_bytes_per_token"],
    autokick_config_default: bool = DEFAULTS["remote_control_autokick"],
    autokick_global_override: bool | None = None,
    autokick_session_overrides: Mapping[str, bool] | None = None,
    autokick_degraded: bool = False,
    live_tmux_sessions: set[str] | None = None,
    # (#32) The subset of live tmux sessions with a client attached — the
    # ones the user has already reopened. None is F16's "could not tell" and
    # never marks a card attached (a failed query must not claim the user is
    # sitting in a session). Resolved ONCE per poll by the caller, like
    # live_tmux_sessions; core does no I/O.
    attached_tmux_sessions: set[str] | None = None,
    reachability_by_sid: Mapping[str, tuple[str, str]] | None = None,
    # (#48) pid -> claude process groups it owns, from the composition
    # root's single per-poll snapshot. None means the probe was not run or
    # could not answer: no conflict is claimed on absent evidence.
    claude_owners: Mapping[int, Sequence[int]] | None = None,
    # (v15, spec 2026-08-21) The dashboard's GLOBAL OAuth auth state — one
    # value for the whole payload, not per-card, because the credentials
    # file `crr.core.auth.auth_state` classifies is shared by every session
    # on this machine. "unknown" is the same "unreadable signal must not
    # become a positive claim" default every other injected classification
    # here uses; the cli resolves the real value from a filesystem read
    # (core must not do it) and passes it in.
    auth_state: str = "unknown",
    auth_expires_in_seconds: int | None = None,
    auth_reauth_url: str | None = None,
) -> dict[str, Any]:
    """Build the /api/sessions payload for ``entries``.

    Entries with ``claude is None`` are registered shells that have no
    claude session yet — live shells, but nothing to rescue — so they are
    not emitted as cards.

    ``context_tight_fraction``/``context_compact_fraction`` are the
    fractions of a model's context window (F2 — compaction badge) at which
    a card's pressure moves from "ok" to "tight" to "will-compact"; the
    caller (cli) reads these from config so this stays pure core with no
    config import (audit: core never imports adapters/cli).

    ``reachability_by_sid`` is the same injection pattern for the card's
    ``remote_control``/``waiting_for`` pair (spec 2026-08-09, Phases 1-3):
    session id -> ``(state, waiting_for)``, where ``state`` is a
    ``contracts.REMOTE_CONTROL_STATES`` member the caller obtained by
    feeding ``session_state.read_all`` through ``reachability.reachability``
    (a filesystem read, so core must not do it). A session with **no
    entry** — nothing injected at all, or no state file for it — defaults
    to ``("unknown", "")``: the absence of a readable signal is not
    evidence the bridge is down, which is the #33 correction restated for
    the new source.

    ``autokick_config_default``/``autokick_global_override``/
    ``autokick_session_overrides`` are the same injection pattern (Slice 3):
    the cli reads ``config.toml``'s ``remote_control_autokick`` and the
    dashboard-managed ``SettingsStore`` (both filesystem reads — core must
    not do either) and passes the resolved values in here, where
    ``settings.autokick_card_state`` turns them into the card's ``autokick``
    field (``"on"``/``"off"``/``"global-off"`` — see
    ``contracts.AUTOKICK_STATES``).

    A degraded settings store no longer produces a lying card: the cli
    passes ``SettingsStore.effective_global_autokick()`` (b4fe3b6), which
    reports False while degraded, so ``autokick`` renders ``"global-off"``
    rather than an ``"on"`` the watchdog is not honouring. The card does
    lose the REASON — the user never turned the global switch off — and the
    Settings modal remains the place that surfaces ``is_degraded()``
    itself. That residual gap is tracked in #40, not here.

    ``live_tmux_sessions`` is the set of tmux session names confirmed
    alive, resolved ONCE per poll by the caller (core does no I/O).
    ``None`` is F16's honest "could not determine" and never promotes an
    entry — an unconfirmed query may not assert that a session is running.

    ``auth_state``/``auth_expires_in_seconds``/``auth_reauth_url`` (v15) are
    the dashboard's GLOBAL OAuth auth fields, injected the same way as
    every other classification here: the cli reads
    ``~/.claude/.credentials.json`` (a filesystem read core must not do)
    and feeds it through ``crr.core.auth.auth_state``, then passes the
    resolved triple in. Defaults are the honest "not resolved" values, not
    a positive claim about credentials this call never saw.
    """
    autokick_session_overrides = autokick_session_overrides or {}
    reachability_by_sid = reachability_by_sid or {}
    sessions = [e for e in entries if e.get("claude") is not None]
    sid_counts = Counter(e["claude"]["session_id"] for e in sessions)

    # Batch the tty probe: one query for every candidate pid instead of one
    # ps per card (DESIGN 'snap jq' perf). classify then reads it O(1).
    tty_pids = process_probe.controlling_ttys([e["pid"] for e in sessions])
    # #48: which entries actually OWN a running claude. Injected, not probed
    # here — the composition root takes ONE process snapshot per poll and
    # feeds it to both this and the reachability detector, because a second
    # `ps -A` per poll is exactly what the batching exists to avoid. Absent
    # (None) means no evidence, and no evidence must not become a claim that
    # two agents are fighting.
    _conflicted = _conflicting_sids(sessions, claude_owners or {})
    probe = _MemoTtyProbe(process_probe, tty_pids)

    cards: list[dict[str, Any]] = []
    for entry in sessions:
        sid = entry["claude"]["session_id"]
        adopted = entry.get("boot_id") == ADOPTED_BOOT_ID
        facts = tail_facts(entry)
        # Defaulting to the module's own constant rather than a third copy
        # of the literal "unknown" — `contracts.REMOTE_CONTROL_STATES` and
        # `reachability`'s constants are bound by a test, so this cannot
        # drift from either.
        reach, waiting_for = reachability_by_sid.get(
            sid, (_reachability.UNKNOWN, "")
        )
        state = _display_state(entry, boot_identity, probe, live_tmux_sessions)
        # (#32) "attached" is only meaningful for a restored (PARKED) card:
        # it separates a session the user has already reopened (a client is
        # attached) from one still merely parked. A None attached set is
        # F16's "could not tell" and never claims attached.
        tmux_name = entry.get("tmux_session")
        attached = bool(
            state == PARKED
            and tmux_name
            and attached_tmux_sessions
            and tmux_name in attached_tmux_sessions
        )
        cards.append(
            {
                "pid": entry["pid"],
                "state": state,
                "attached": attached,
                "cwd": entry["cwd"],
                # (#40) An ADOPTED entry never observed a shell registration
                # — `build_adopted_entry` writes host="tab"/shell="bash"
                # because the v1 schema admits no None, and its docstring is
                # explicit that "any enum member the schema accepts would be
                # equally fabricated". Those two fields are display-only
                # (nothing in crr decides on them), so the dashboard is the
                # only place the fabrication could escape to — and it stops
                # here. The journal keeps its schema-valid filler; the card
                # reports what was actually seen, which is nothing.
                "shell": "" if adopted else entry["shell"],
                "host": "" if adopted else entry["host"],
                "session_id": sid,
                "sid_source": entry["claude"]["sid_source"],
                "sid8": sid[:8],
                "last_prompt": facts["last_prompt"],
                "last_reply": facts["last_reply"],
                "title": facts["title"],
                "slug": facts["slug"],
                "model": facts["model"],
                "duplicate_group": sid if sid_counts[sid] > 1 else None,
                "conflict": sid in _conflicted,
                "tmux_session": entry["tmux_session"],
                "updated": entry["updated"],
                "last_active": facts["last_active"],
                "context_pressure": _pressure(
                    facts["transcript_bytes"],
                    facts["model"],
                    tight=context_tight_fraction,
                    compact=context_compact_fraction,
                    bytes_per_token=context_bytes_per_token,
                ),
                "remote_control": reach,
                "waiting_for": waiting_for,
                "autokick": _settings.autokick_card_state(
                    config_default=autokick_config_default,
                    global_override=autokick_global_override,
                    session_override=autokick_session_overrides.get(sid),
                    degraded=autokick_degraded,
                ),
                "adopted": adopted,
                "skip_permissions": entry["claude"].get("skip_permissions", False),
                "revive_strikes": entry["revive_strikes"],
            }
        )

    return {
        "contract": contracts.SESSIONS_CONTRACT_VERSION,
        "sessions": cards,
        "auth_state": auth_state,
        "auth_expires_in_seconds": auth_expires_in_seconds,
        "auth_reauth_url": auth_reauth_url,
    }
