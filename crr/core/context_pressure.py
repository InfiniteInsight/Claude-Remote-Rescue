"""Context-pressure estimation (Slice A, Task A2 — F2 compaction badge).

Answers "is this session close to compacting on revive?" from two honestly
labeled inputs:

- ``estimate_tokens``: a rough ESTIMATE of a transcript's token count from
  its byte size (``bytes // 4``) — not a real tokenizer count.
- ``MODEL_CONTEXT_WINDOWS``: model name -> context window size in tokens.
  Every listed entry is now confirmed against published model docs (each
  carries a ``# confirmed`` provenance comment). Any model NOT in the map
  falls back to ``DEFAULT_WINDOW`` (200_000): wrong-but-conservative beats
  confidently-wrong, so an unknown model's badge under-warns rather than
  fabricates a window. A new model joins the map only with a real source.

Pure core: stdlib only, no I/O.
"""

from __future__ import annotations

# Fallback context window (tokens) for any model not in the map below, and
# for models we are genuinely unsure about (see comments per entry).
DEFAULT_WINDOW = 200_000

# model -> context window size in tokens. All entries confirmed from released
# model documentation (see each entry's provenance comment); a model absent
# from this map falls back to DEFAULT_WINDOW rather than a fabricated number.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 1_000_000,  # confirmed
    "claude-opus-5": 1_000_000,  # confirmed (user, 2026-08)
    "claude-sonnet-5": 1_000_000,  # confirmed (user, 2026-08)
    "claude-sonnet-4-6": 1_000_000,  # confirmed (web, 2026-08): Sonnet 4.6 = 1M GA
    "claude-haiku-4-5-20251001": 200_000,  # confirmed (web, 2026-08): Haiku 4.5 = 200K (smaller tier)
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
    """Classify context pressure as ``"unknown"``, ``"ok"``, ``"tight"``, or
    ``"will-compact"`` from the estimated token fraction of the model's
    context window.

    - ``"unknown"``: ``model`` is not in ``MODEL_CONTEXT_WINDOWS`` — either
      the empty string ``read_tail_facts`` reports when no model could be
      extracted, or a real model this build has no confirmed window for.
    - ``"ok"``: fraction < ``tight``
    - ``"tight"``: ``tight`` <= fraction < ``compact``
    - ``"will-compact"``: fraction >= ``compact``

    The ``"unknown"`` arm is #39. ``window_for`` keeps its conservative
    ``DEFAULT_WINDOW`` fallback — sensible for a lookup — but letting that
    fallback flow through here put a fabricated denominator behind a badge
    the card rendered identically to a confirmed one. That is not a rare
    edge: ``config.py``'s ``model_tail_lines`` comment records the measured
    rate as "~1 in 3 transcripts carry NO model at all". A third of badges
    were claims about a context window nobody had established. An honest
    null beats a hedged number, so the estimate is simply not made.
    """
    if model not in MODEL_CONTEXT_WINDOWS:
        return "unknown"
    fraction = estimate_tokens(transcript_bytes) / window_for(model)
    if fraction >= compact:
        return "will-compact"
    if fraction >= tight:
        return "tight"
    return "ok"
