"""Shared subprocess plumbing for adapters.

One home for the "run a command, return stdout, RAISE on a nonzero exit"
pattern the diagnostics sources rely on — the guard that keeps a swallowed
exit code from masquerading as an empty-but-successful result.

It is also the one home for the power holders' release escalation ladder
(``reaped`` / ``signal_child`` / ``release_child``). The Windows and Linux
holders were fixed for the SAME defect one after another — ``release()``
dropping its handle to a child unconditionally, even one that never died —
and two copies of that ladder would only drift apart again. Both holders
call ``release_child``; there is exactly one place the escalation logic
can go wrong.
"""

from __future__ import annotations

import subprocess
from typing import Sequence


def run_capture(argv: Sequence[str], timeout: float) -> str:
    """Run ``argv``, returning stdout; raise ``RuntimeError`` on nonzero exit."""
    result = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{argv[0]} exited {result.returncode}")
    return result.stdout


# Teardown budget shared by every holder's release() ladder. Measured
# Windows normal teardown (stdin close -> EOF -> both release calls ->
# [Environment]::Exit(0)) is ~2.07s, so blowing the first wait means
# something is genuinely wrong and escalating beats swallowing it: the
# child may still hold a power lock. Kept as ONE pair of numbers so the
# platforms cannot drift to different patience.
RELEASE_WAIT_SECONDS = 10
FORCE_WAIT_SECONDS = 5


def reaped(proc, timeout: float) -> bool:
    """True only when ``wait`` actually returned -- i.e. confirmed dead.

    A caller must never treat a signal call (``terminate``/``kill``) alone
    as proof of death: the process may ignore it, and ``wait`` timing out
    is the only honest way to find that out.
    """
    try:
        proc.wait(timeout=timeout)
        return True
    except Exception:
        return False


def signal_child(proc, name: str) -> None:
    """Best-effort ``terminate``/``kill``. A process that already exited
    (or a stub missing the method) must not blow up the ladder -- the
    caller re-checks with ``reaped`` regardless of whether this raised."""
    try:
        getattr(proc, name)()
    except Exception:
        pass


def release_child(proc, first_wait: float, force_wait: float) -> bool:
    """Escalate terminate -> kill until CONFIRMED reaped; True only then.

    ``first_wait`` gives whatever graceful request the caller already made
    (e.g. closing a child's stdin, or its own initial ``terminate``) a
    chance to work before this escalates further. Deliberately no
    ``finally`` around any of this -- the caller decides what "not
    reaped" means for its own bookkeeping (keep the handle, keep
    reporting the hold), and a ``finally`` here is exactly the shape that
    papers over that decision and reinstates the "released" lie.
    """
    if reaped(proc, first_wait):
        return True
    signal_child(proc, "terminate")
    if reaped(proc, force_wait):
        return True
    signal_child(proc, "kill")
    return reaped(proc, force_wait)
