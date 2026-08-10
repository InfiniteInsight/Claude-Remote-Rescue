"""Status assembler — journal entries -> /api/sessions payload (contract v12).

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
    reachability_by_sid: Mapping[str, tuple[str, str]] | None = None,
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
    """
    autokick_session_overrides = autokick_session_overrides or {}
    reachability_by_sid = reachability_by_sid or {}
    sessions = [e for e in entries if e.get("claude") is not None]
    sid_counts = Counter(e["claude"]["session_id"] for e in sessions)

    # Batch the tty probe: one query for every candidate pid instead of one
    # ps per card (DESIGN 'snap jq' perf). classify then reads it O(1).
    tty_pids = process_probe.controlling_ttys([e["pid"] for e in sessions])
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
        cards.append(
            {
                "pid": entry["pid"],
                "state": _display_state(
                    entry, boot_identity, probe, live_tmux_sessions
                ),
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
            }
        )

    return {"contract": contracts.SESSIONS_CONTRACT_VERSION, "sessions": cards}
