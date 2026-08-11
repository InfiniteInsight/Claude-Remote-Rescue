"""Mutation-lock tests — serialize journal/archive read-modify-write.

Atomic file writes prevent *corruption* but not *logical* races: two
threads (ThreadingHTTPServer) or two processes (`crr web` vs the revive
timer) can both read, both decide, both write. The lock closes that. GET
polls don't take it; only mutations do.
"""

import pytest
import threading
import time

from crr.adapters.locking import mutation_lock


def test_lock_serializes_read_modify_write(tmp_path):
    # Without the lock this classic RMW loses updates; with it, none are lost.
    state = {"counter": 0}

    def bump():
        for _ in range(20):
            with mutation_lock(tmp_path):
                tmp = state["counter"]
                time.sleep(0.0005)  # widen the race window
                state["counter"] = tmp + 1

    threads = [threading.Thread(target=bump) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["counter"] == 5 * 20  # no lost updates


def test_lock_is_reentrant_across_sequential_uses(tmp_path):
    with mutation_lock(tmp_path):
        pass
    with mutation_lock(tmp_path):  # re-acquire after release must not deadlock
        pass


def test_lock_creates_lockfile_under_state_dir(tmp_path):
    with mutation_lock(tmp_path):
        assert (tmp_path / ".crr.lock").exists()


# --- the Windows backend's waiting policy (#70) ---------------------------
#
# crr could not be imported on Windows at all: fcntl at module scope killed
# every entry point. The port matters more than the import — a lock that
# gave up under contention would turn a busy moment into interleaved
# read-modify-writes on the journal, which is what it exists to prevent.
# The msvcrt call needs Windows; the waiting behaviour does not, so it is
# injected here and verified everywhere.

def test_windows_acquire_returns_as_soon_as_the_range_is_free():
    from crr.adapters import locking
    calls = []
    locking._acquire_windows(3, attempt=lambda fd: calls.append(fd), sleep=lambda s: None)
    assert calls == [3]


def test_windows_acquire_waits_out_a_held_lock_instead_of_failing():
    from crr.adapters import locking
    state = {"held": 3}
    slept = []

    def attempt(fd):
        if state["held"]:
            state["held"] -= 1
            raise OSError(13, "Permission denied")

    locking._acquire_windows(9, attempt=attempt, sleep=slept.append)
    assert len(slept) == 3, "gave up instead of waiting for the holder"
    assert all(s == locking._WINDOWS_RETRY_SECONDS for s in slept)


def test_windows_acquire_waits_indefinitely_by_default():
    # POSIX flock(LOCK_EX) blocks forever; so does this. A bounded wait
    # would make contention look like failure.
    from crr.adapters import locking
    state = {"n": 0}

    def attempt(fd):
        state["n"] += 1
        if state["n"] < 500:
            raise OSError(13, "Permission denied")

    locking._acquire_windows(1, attempt=attempt, sleep=lambda s: None)
    assert state["n"] == 500


def test_windows_acquire_can_be_bounded_for_tests_and_reraises():
    from crr.adapters import locking

    def always_held(fd):
        raise OSError(13, "Permission denied")

    with pytest.raises(OSError):
        locking._acquire_windows(1, attempt=always_held, sleep=lambda s: None,
                                 max_attempts=3)


def test_the_module_imports_without_fcntl_available(monkeypatch):
    # The actual #70 defect: `import fcntl` at module scope meant crr.cli —
    # and therefore every entry point — died at import on Windows.
    import importlib
    import sys
    from crr.adapters import locking

    real = sys.modules.pop("fcntl", None)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    try:
        # Re-importing with fcntl poisoned must still work on nt; on POSIX
        # the guard is that the import is inside the os.name branch at all.
        assert "import fcntl" not in _module_toplevel_imports(locking)
    finally:
        if real is not None:
            sys.modules["fcntl"] = real


def _module_toplevel_imports(module) -> str:
    """The module's imports that run unconditionally at import time."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    out = []
    for node in tree.body:  # top level only — not inside if/try/def
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(ast.unparse(node))
    return "\n".join(out)
