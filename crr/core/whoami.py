"""Which crr session is this process running inside? (`crr whoami`)

The mobile/remote Claude list shows a title but no session id and no cwd,
so tying a conversation to a crr card means asking from inside it. A Bash
tool call made by Claude is a descendant of the journaled shell:

    python -> bash -> claude --resume <sid> -> fish   <- journaled by crr

so walking up the process tree until a journaled pid appears identifies the
session exactly — no guessing by cwd, no matching on prompt text.

Pure core: the parent lookup is injected, so the walk is testable without
real processes (the adapter supplies a real ``ps``-backed lookup).
"""

from __future__ import annotations

from typing import Callable, Iterable

# Bound on the walk. A real chain is ~3-4 hops; anything deeper means a
# pathological tree (or a cycle), and a diagnostic command must terminate
# rather than hang.
MAX_HOPS = 64


def journaled_ancestor(
    pid: int, journaled_pids: Iterable[int], parent_of: Callable[[int], int | None]
) -> int | None:
    """The nearest journaled pid at or above ``pid``, or None.

    ``parent_of`` returns the parent pid, or None once the walk runs out
    (root reached, process gone, or the probe failed). A pid that parents
    itself — or any cycle — terminates via the visited set rather than
    looping forever.
    """
    journaled = set(journaled_pids)
    seen: set[int] = set()
    current: int | None = pid
    for _ in range(MAX_HOPS):
        if current is None or current <= 1 or current in seen:
            return None
        if current in journaled:
            return current
        seen.add(current)
        current = parent_of(current)
    return None
