"""Context-pressure tests (Slice A, Task A2 — F2 compaction badge).

``pressure`` turns a transcript's byte size + model name into a coarse
"how close to compaction" level. The token count is an ESTIMATE (bytes//4)
and the model->window map is a documented PRIOR (audit P5) — these tests
pin the thresholds and the honest fallbacks, not real token accuracy.
"""

from crr.core import context_pressure as cp


def test_estimate_tokens_is_bytes_over_four():
    assert cp.estimate_tokens(0) == 0
    assert cp.estimate_tokens(4) == 1
    assert cp.estimate_tokens(400_000) == 100_000
    # floor division — an estimate, not exact.
    assert cp.estimate_tokens(7) == 1


def test_window_for_known_model():
    assert cp.window_for("claude-opus-4-8") == 1_000_000


def test_window_for_unknown_model_falls_back_to_default():
    assert cp.window_for("some-model-nobody-heard-of") == cp.DEFAULT_WINDOW
    assert cp.window_for("") == cp.DEFAULT_WINDOW


def test_confirmed_model_windows():
    # Confirmed against published docs (web, 2026-08): the 1M-context tier...
    for model in (
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-fable-5",
    ):
        assert cp.window_for(model) == 1_000_000, model
    # ...and Claude Haiku 4.5, confirmed at 200K (smaller than the 1M tier).
    assert cp.window_for("claude-haiku-4-5-20251001") == 200_000


def test_default_window_is_200k():
    assert cp.DEFAULT_WINDOW == 200_000


def test_all_documented_models_present_and_positive():
    expected = {
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-fable-5",
    }
    assert expected <= set(cp.MODEL_CONTEXT_WINDOWS)
    for model, window in cp.MODEL_CONTEXT_WINDOWS.items():
        assert isinstance(window, int)
        assert window > 0


def test_pressure_ok_below_tight_threshold():
    # window 200_000, tight=0.7 -> ok below 140_000 tokens -> below 560_000 bytes
    level = cp.pressure(400_000, "unknown-model", tight=0.7, compact=1.0)
    assert level == "ok"


def test_pressure_tight_at_lower_boundary_inclusive():
    # fraction exactly == tight -> "tight" (tight <= fraction < compact)
    window = cp.window_for("unknown-model")
    tokens_at_boundary = int(window * 0.7)
    transcript_bytes = tokens_at_boundary * 4
    level = cp.pressure(transcript_bytes, "unknown-model", tight=0.7, compact=1.0)
    assert level == "tight"


def test_pressure_tight_just_below_compact():
    window = cp.window_for("unknown-model")
    transcript_bytes = int(window * 0.99) * 4
    level = cp.pressure(transcript_bytes, "unknown-model", tight=0.7, compact=1.0)
    assert level == "tight"


def test_pressure_will_compact_at_upper_boundary_inclusive():
    # fraction exactly == compact -> "will-compact" (fraction >= compact)
    window = cp.window_for("unknown-model")
    transcript_bytes = window * 4
    level = cp.pressure(transcript_bytes, "unknown-model", tight=0.7, compact=1.0)
    assert level == "will-compact"


def test_pressure_will_compact_over_window():
    level = cp.pressure(50_000_000, "claude-opus-4-8", tight=0.7, compact=1.0)
    assert level == "will-compact"


def test_pressure_ok_for_opus_4_8_with_large_but_under_window_transcript():
    # opus-4-8 window is 1_000_000 tokens -> 4_000_000 bytes. 1MB transcript
    # is well under that (250k tokens, fraction 0.25) -> ok.
    level = cp.pressure(1_000_000, "claude-opus-4-8", tight=0.7, compact=1.0)
    assert level == "ok"
