"""Mutation lock — serialize journal/archive read-modify-write.

An exclusive advisory file lock held for the duration of a mutating
operation, so concurrent writers — ``crr web``'s handler threads and the
separate revive-timer process — cannot interleave a read, a decision, and a
write. Atomic writes keep individual files uncorrupted; this keeps
*operations* atomic against each other.

Read-only paths (status polling, scan) deliberately do NOT take the lock —
atomic writes already give readers a consistent view, and locking the poll
path would serialize the dashboard against every mutation.

This is an adapter (an OS resource), not core: the lock is acquired by the
composition root around core operations, keeping core pure and lock-free.

Two backends, one contract (#70). POSIX uses ``fcntl.flock``. Windows uses
``msvcrt.locking`` on a one-byte range, retried until acquired — Windows
has no blocking-until-free primitive here that also releases correctly on
abnormal exit, and ``LK_LOCK`` gives up after ten one-second attempts,
which would surface as a spurious failure under exactly the contention the
lock exists for. The retry loop makes the waiting policy explicit rather
than inheriting an undocumented one.

Both backends release on process death: POSIX flock is tied to the open
file description, Windows byte-range locks to the file handle, and both are
freed when the kernel closes the file. That matters more than elegance —
crr's whole purpose is surviving processes that die badly.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

_LOCK_NAME = ".crr.lock"

# How long to nap between Windows acquisition attempts. Short enough that a
# brief mutation is not perceptibly delayed, long enough that waiting costs
# no measurable CPU. A named prior, not an inline magic number.
_WINDOWS_RETRY_SECONDS = 0.02

if os.name == "nt":  # pragma: no cover - exercised on Windows CI
    import msvcrt

    def _acquire(fd: int) -> None:
        _acquire_windows(fd)

    def _release(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _acquire(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _release(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _acquire_windows(
    fd: int,
    *,
    attempt: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int | None = None,
) -> None:
    """Take an exclusive one-byte lock on ``fd``, waiting until it is free.

    ``LK_NBLCK`` fails immediately when the range is held, so waiting is
    this loop's job. It waits indefinitely by default, which is what POSIX
    ``flock(LOCK_EX)`` does — a mutation lock that gave up would turn
    contention into data loss, which is the opposite of the point.

    ``attempt``/``sleep``/``max_attempts`` exist so the waiting behaviour is
    testable off Windows; nothing in the product passes them.
    """
    if attempt is None:  # pragma: no cover - the msvcrt path needs Windows
        def attempt(handle: int) -> None:
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)

    tries = 0
    while True:
        try:
            attempt(fd)
            return
        except OSError:
            tries += 1
            if max_attempts is not None and tries >= max_attempts:
                raise
            sleep(_WINDOWS_RETRY_SECONDS)


@contextmanager
def mutation_lock(state_dir: Path | str) -> Iterator[None]:
    """Hold an exclusive lock on ``state_dir`` for the duration of the block.

    A fresh file descriptor per acquisition means the lock contends
    correctly both across threads (each gets its own open file description
    / handle) and across processes.
    """
    lock_path = Path(state_dir) / _LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _acquire(fd)
        yield
    finally:
        try:
            _release(fd)
        finally:
            os.close(fd)
