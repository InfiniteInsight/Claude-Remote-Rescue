"""Status assembler — journal entries -> /api/sessions payload (contract v4).

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
from crr.core import settings as _settings
from crr.core.bridge import bridge_state as _bridge_state
from crr.core.classifier import classify
from crr.core.context_pressure import pressure as _pressure
from crr.core.ports import BootIdentity, ProcessProbe


def _empty_facts(_entry: Mapping[str, Any]) -> dict[str, Any]:
    return {"last_prompt": "", "model": "", "last_active": "",
            "last_reply": "", "title": "", "slug": "", "transcript_bytes": 0,
            "bridge_seen": False, "bridge_since": 0}


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


def assemble_sessions(
    entries: Sequence[Mapping[str, Any]],
    boot_identity: BootIdentity,
    process_probe: ProcessProbe,
    *,
    tail_facts: Callable[[Mapping[str, Any]], dict[str, Any]] = _empty_facts,
    context_tight_fraction: float = 0.7,
    context_compact_fraction: float = 1.0,
    bridge_stale_records: int = 150,
    autokick_config_default: bool = True,
    autokick_global_override: bool | None = None,
    autokick_session_overrides: Mapping[str, bool] | None = None,
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

    ``bridge_stale_records`` is the same kind of injected threshold, for
    the dropped-Remote-Control badge (spec 2026-08-07): the caller reads
    ``bridge_stale_records`` from config and passes it in here, and
    ``bridge.bridge_state`` turns it plus ``tail_facts``'s
    ``bridge_seen``/``bridge_since`` into the card's ``remote_control``
    value.

    ``autokick_config_default``/``autokick_global_override``/
    ``autokick_session_overrides`` are the same injection pattern (Slice 3):
    the cli reads ``config.toml``'s ``remote_control_autokick`` and the
    dashboard-managed ``SettingsStore`` (both filesystem reads — core must
    not do either) and passes the resolved values in here, where
    ``settings.autokick_card_state`` turns them into the card's ``autokick``
    field (``"on"``/``"off"``/``"global-off"`` — see
    ``contracts.AUTOKICK_STATES``).

    Known gap, accepted for this slice: when the dashboard's settings file
    is unreadable, ``SettingsStore.is_degraded()`` is True and the watchdog
    auto-kicks NOTHING (fail-closed, Slice 2) — but that degraded state is
    not plumbed into this function, so a card can still read ``autokick:
    "on"`` while nothing is actually being kicked. The Settings modal
    surfaces ``is_degraded()`` directly (Slice 3 deliverable #2) so the user
    sees the real reason there; teaching every card about it too was judged
    out of scope for this slice.
    """
    autokick_session_overrides = autokick_session_overrides or {}
    sessions = [e for e in entries if e.get("claude") is not None]
    sid_counts = Counter(e["claude"]["session_id"] for e in sessions)

    # Batch the tty probe: one query for every candidate pid instead of one
    # ps per card (DESIGN 'snap jq' perf). classify then reads it O(1).
    tty_pids = process_probe.controlling_ttys([e["pid"] for e in sessions])
    probe = _MemoTtyProbe(process_probe, tty_pids)

    cards: list[dict[str, Any]] = []
    for entry in sessions:
        sid = entry["claude"]["session_id"]
        facts = tail_facts(entry)
        cards.append(
            {
                "pid": entry["pid"],
                "state": classify(entry, boot_identity, probe),
                "cwd": entry["cwd"],
                "shell": entry["shell"],
                "host": entry["host"],
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
                ),
                "remote_control": _bridge_state(
                    facts["bridge_since"],
                    facts["bridge_seen"],
                    stale_after=bridge_stale_records,
                ),
                "autokick": _settings.autokick_card_state(
                    config_default=autokick_config_default,
                    global_override=autokick_global_override,
                    session_override=autokick_session_overrides.get(sid),
                ),
            }
        )

    return {"contract": contracts.SESSIONS_CONTRACT_VERSION, "sessions": cards}
