"""Mutation-lock tests — serialize journal/archive read-modify-write.

Atomic file writes prevent *corruption* but not *logical* races: two
threads (ThreadingHTTPServer) or two processes (`crr web` vs the revive
timer) can both read, both decide, both write. The lock closes that. GET
polls don't take it; only mutations do.
"""

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
