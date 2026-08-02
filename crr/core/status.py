"""Status assembler — journal entries -> /api/sessions payload (contract v3).

Pure core: takes already-scanned entries plus the BootIdentity and
ProcessProbe ports, classifies each entry, and emits the versioned
sessions payload. ``sid_source`` is copied onto every card so the
dashboard can weight ``guessed`` claims when it renders duplicate groups
(audit P3 — confidence + provenance travel with the data).

Duplicate detection here is deliberately simple and honest: entries that
journal the *same* ``session_id`` form a group. The refinement of
down-weighting a ``guessed`` claim against an ``injected`` one is the
dashboard's job — the raw provenance it needs is on each card.

``last_prompt`` and ``model`` come from one injected ``tail_facts``
extractor that reads both in a single backward pass over the transcript
(both live near the tail). The default returns empty strings — an honest
"not extracted", never a fabricated line or model.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Mapping, Sequence

from crr.core import contracts
from crr.core.classifier import classify
from crr.core.ports import BootIdentity, ProcessProbe


def _empty_facts(_entry: Mapping[str, Any]) -> dict[str, str]:
    return {"last_prompt": "", "model": ""}


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
    tail_facts: Callable[[Mapping[str, Any]], dict[str, str]] = _empty_facts,
) -> dict[str, Any]:
    """Build the /api/sessions payload for ``entries``.

    Entries with ``claude is None`` are registered shells that have no
    claude session yet — live shells, but nothing to rescue — so they are
    not emitted as cards.
    """
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
                "model": facts["model"],
                "duplicate_group": sid if sid_counts[sid] > 1 else None,
                "tmux_session": entry["tmux_session"],
                "updated": entry["updated"],
            }
        )

    return {"contract": contracts.SESSIONS_CONTRACT_VERSION, "sessions": cards}
