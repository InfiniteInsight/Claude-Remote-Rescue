"""Context-pressure estimation (Slice A, Task A2 — F2 compaction badge).

Answers "is this session close to compacting on revive?" from two honestly
labeled inputs:

- ``estimate_tokens``: a rough ESTIMATE of a transcript's token count from
  its byte size (``bytes // 4``) — not a real tokenizer count.
- ``MODEL_CONTEXT_WINDOWS``: a documented PRIOR (audit P5) mapping model
  name -> context window size in tokens. Only ``claude-opus-4-8`` is
  confirmed; every other entry is a best-guess placeholder marked
  ``# PRIOR — verify`` and MUST be checked against real model docs before
  being trusted. Wrong-but-conservative beats confidently-wrong, so unsure
  entries use ``DEFAULT_WINDOW`` (200_000) rather than a fabricated number.

Pure core: stdlib only, no I/O.
"""

from __future__ import annotations

# Fallback context window (tokens) for any model not in the map below, and
# for models we are genuinely unsure about (see comments per entry).
DEFAULT_WINDOW = 200_000

# PRIOR (audit P5): model -> context window size in tokens. Only the first
# entry is confirmed from released model documentation; the rest are
# placeholders for models observed in the wild at write time and must be
# verified against real published specs, not treated as ground truth.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 1_000_000,  # confirmed
    "claude-opus-5": 1_000_000,  # confirmed (user, 2026-08)
    "claude-sonnet-5": 1_000_000,  # confirmed (user, 2026-08)
    "claude-sonnet-4-6": DEFAULT_WINDOW,  # PRIOR — verify
    "claude-haiku-4-5-20251001": DEFAULT_WINDOW,  # PRIOR — verify (haiku is smaller)
    "claude-fable-5": 1_000_000,  # confirmed (user, 2026-08)
}


def estimate_tokens(transcript_bytes: int) -> int:
    """Rough ESTIMATE of token count from transcript byte size.

    Not a real tokenizer count — a cheap ``bytes // 4`` heuristic used only
    to gauge context pressure at a coarse grain.
    """
    return transcript_bytes // 4


def window_for(model: str) -> int:
    """Look up ``model``'s context window (tokens), falling back to
    ``DEFAULT_WINDOW`` for anything unmapped."""
    return MODEL_CONTEXT_WINDOWS.get(model, DEFAULT_WINDOW)


def pressure(transcript_bytes: int, model: str, *, tight: float, compact: float) -> str:
    """Classify context pressure as ``"ok"``, ``"tight"``, or
    ``"will-compact"`` from the estimated token fraction of the model's
    context window.

    - ``"ok"``: fraction < ``tight``
    - ``"tight"``: ``tight`` <= fraction < ``compact``
    - ``"will-compact"``: fraction >= ``compact``
    """
    fraction = estimate_tokens(transcript_bytes) / window_for(model)
    if fraction >= compact:
        return "will-compact"
    if fraction >= tight:
        return "tight"
    return "ok"
