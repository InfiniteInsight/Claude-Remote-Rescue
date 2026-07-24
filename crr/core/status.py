"""Status assembler — journal entries -> /api/sessions payload (contract v1).

Pure core: takes already-scanned entries plus the BootIdentity and
ProcessProbe ports, classifies each entry, and emits the versioned
sessions payload. ``sid_source`` is copied onto every card so the
dashboard can weight ``guessed`` claims when it renders duplicate groups
(audit P3 — confidence + provenance travel with the data).

Duplicate detection here is deliberately simple and honest: entries that
journal the *same* ``session_id`` form a group. The refinement of
down-weighting a ``guessed`` claim against an ``injected`` one is the
dashboard's job — the raw provenance it needs is on each card.

``last_prompt`` is supplied by an injected extractor. The transcript
reader (with its skip-list) is a later increment; until it is wired the
default extractor returns "" — an honest "not extracted", never a
fabricated line.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Mapping, Sequence

from crr.core import contracts
from crr.core.classifier import classify
from crr.core.ports import BootIdentity, ProcessProbe


def _empty_prompt(_entry: Mapping[str, Any]) -> str:
    return ""


def assemble_sessions(
    entries: Sequence[Mapping[str, Any]],
    boot_identity: BootIdentity,
    process_probe: ProcessProbe,
    *,
    last_prompt: Callable[[Mapping[str, Any]], str] = _empty_prompt,
) -> dict[str, Any]:
    """Build the /api/sessions payload for ``entries``."""
    sid_counts = Counter(e["claude"]["session_id"] for e in entries)

    cards: list[dict[str, Any]] = []
    for entry in entries:
        sid = entry["claude"]["session_id"]
        cards.append(
            {
                "pid": entry["pid"],
                "state": classify(entry, boot_identity, process_probe),
                "cwd": entry["cwd"],
                "shell": entry["shell"],
                "host": entry["host"],
                "session_id": sid,
                "sid_source": entry["claude"]["sid_source"],
                "sid8": sid[:8],
                "last_prompt": last_prompt(entry),
                "duplicate_group": sid if sid_counts[sid] > 1 else None,
                "updated": entry["updated"],
            }
        )

    return {"contract": contracts.SESSIONS_CONTRACT_VERSION, "sessions": cards}
