"""Shared adapter subprocess helper (crr.adapters._proc)."""

import pytest

from crr.adapters import _proc


def test_run_capture_returns_stdout_on_success():
    assert _proc.run_capture(["printf", "hello"], timeout=5) == "hello"


def test_run_capture_raises_on_nonzero_exit():
    # The "never swallow a nonzero exit" guard the diagnostics sources rely on.
    with pytest.raises(RuntimeError):
        _proc.run_capture(["sh", "-c", "echo boom >&2; exit 3"], timeout=5)
