import os

import pytest


@pytest.fixture(autouse=True)
def crr_state(tmp_path, monkeypatch):
    """Isolated state dir per test, with CRR_* env scrubbed first.

    [lesson: env leakage] Anything spawned from a revived tmux session
    inherits the revival environment; the test runner must scrub CRR_*
    control variables rather than trusting a clean environment.
    """
    for key in list(os.environ):
        if key.startswith("CRR_"):
            monkeypatch.delenv(key, raising=False)
    state = tmp_path / "state"
    monkeypatch.setenv("CRR_STATE_DIR", str(state))
    return state
