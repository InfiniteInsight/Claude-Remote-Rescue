"""Mutation lock — serialize journal/archive read-modify-write.

An exclusive advisory file lock (``fcntl.flock``) held for the duration of
a mutating operation, so concurrent writers — ``crr web``'s handler
threads and the separate revive-timer process — cannot interleave a read,
a decision, and a write. Atomic writes keep individual files uncorrupted;
this keeps *operations* atomic against each other.

Read-only paths (status polling, scan) deliberately do NOT take the lock —
atomic writes already give readers a consistent view, and locking the poll
path would serialize the dashboard against every mutation.

This is an adapter (an OS resource, POSIX-only), not core: the lock is
acquired by the composition root around core operations, keeping core
pure and lock-free. A Windows equivalent arrives with that platform.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_LOCK_NAME = ".crr.lock"


@contextmanager
def mutation_lock(state_dir: Path | str) -> Iterator[None]:
    """Hold an exclusive lock on ``state_dir`` for the duration of the block.

    A fresh file descriptor per acquisition means flock contends correctly
    both across threads (each gets its own open file description) and across
    processes.
    """
    lock_path = Path(state_dir) / _LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
