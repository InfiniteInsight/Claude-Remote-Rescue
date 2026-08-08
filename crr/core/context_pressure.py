"""Context-pressure estimation (Slice A, Task A2 — F2 compaction badge).

Answers "is this session close to compacting on revive?" from two honestly
labeled inputs:

- ``estimate_tokens``: a rough ESTIMATE of a transcript's token count from
  its byte size — not a real tokenizer count. The divisor is the injectable
  ``context_bytes_per_token`` prior (#37), not a buried ``// 4``: it is the
  single most load-bearing assumption behind every badge this module
  produces, and it varies with content (code and JSON pack denser than
  prose), so it belongs where it can be seen and changed.
- ``MODEL_CONTEXT_WINDOWS``: model name -> context window size in tokens.
  Every listed entry is confirmed against published model docs (each
  carries a ``# confirmed`` provenance comment). A model NOT in the map has
  no window, and ``window_for`` says so with ``None`` — there is no
  fallback and no fabricated number. A new model joins only with a source.

There used to be a ``DEFAULT_WINDOW`` fallback here, justified as
"wrong-but-conservative beats confidently-wrong". #39 removed its only
consumer (``pressure`` now returns ``"unknown"`` rather than dividing by a
guess), which left it influencing no decision at all — a dead prior, not
an injectable one. #37 deleted it rather than promoting it to config: the
honest answer to "what is this model's context window?" is ``None``.

Pure core: stdlib only, no I/O.
"""

from __future__ import annotations

from crr.core.config import DEFAULTS

# model -> context window size in tokens. All entries confirmed from released
# model documentation (see each entry's provenance comment); a model absent
# from this map has no known window (``window_for`` -> None).
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 1_000_000,  # confirmed
    "claude-opus-5": 1_000_000,  # confirmed (user, 2026-08)
    "claude-sonnet-5": 1_000_000,  # confirmed (user, 2026-08)
    "claude-sonnet-4-6": 1_000_000,  # confirmed (web, 2026-08): Sonnet 4.6 = 1M GA
    "claude-haiku-4-5-20251001": 200_000,  # confirmed (web, 2026-08): Haiku 4.5 = 200K (smaller tier)
    "claude-fable-5": 1_000_000,  # confirmed (user, 2026-08)
}


def estimate_tokens(
    transcript_bytes: int, bytes_per_token: int = DEFAULTS["context_bytes_per_token"],
) -> int:
    """Rough ESTIMATE of token count from transcript byte size.

    Not a real tokenizer count — a cheap bytes-per-token heuristic used only
    to gauge context pressure at a coarse grain. ``bytes_per_token`` is the
    injectable prior (``context_bytes_per_token``); the default is read from
    ``config.DEFAULTS`` rather than repeated here, so the two cannot drift.
    """
    return transcript_bytes // bytes_per_token


def window_for(model: str) -> int | None:
    """``model``'s context window in tokens, or ``None`` if unknown.

    ``None`` rather than a fallback constant: a model absent from the map is
    one this build has no confirmed window for, and a number invented to
    stand in for that would be exactly the fabricated denominator #39 was
    about. Callers decide what an unknown window means for them — ``pressure``
    declines to classify at all.
    """
    return MODEL_CONTEXT_WINDOWS.get(model)


def pressure(
    transcript_bytes: int, model: str, *, tight: float, compact: float,
    bytes_per_token: int = DEFAULTS["context_bytes_per_token"],
) -> str:
    """Classify context pressure as ``"unknown"``, ``"ok"``, ``"tight"``, or
    ``"will-compact"`` from the estimated token fraction of the model's
    context window.

    - ``"unknown"``: ``window_for(model)`` is None — either the empty string
      ``read_tail_facts`` reports when no model could be extracted, or a
      real model this build has no confirmed window for.
    - ``"ok"``: fraction < ``tight``
    - ``"tight"``: ``tight`` <= fraction < ``compact``
    - ``"will-compact"``: fraction >= ``compact``

    The ``"unknown"`` arm is #39: an unmapped model used to borrow a
    fallback window, putting a fabricated denominator behind a badge the
    card rendered identically to a confirmed one. Not a rare edge —
    ``config.py``'s ``model_tail_lines`` comment records the measured rate
    as "~1 in 3 transcripts carry NO model at all", so a third of badges
    were claims about a window nobody had established. An honest null beats
    a hedged number, so the estimate is simply not made.

    ``tight``/``compact``/``bytes_per_token`` are all injected priors (#37);
    the caller (cli) reads them from config so this stays pure core.
    """
    window = window_for(model)
    if window is None:
        return "unknown"
    fraction = estimate_tokens(transcript_bytes, bytes_per_token) / window
    if fraction >= compact:
        return "will-compact"
    if fraction >= tight:
        return "tight"
    return "ok"
