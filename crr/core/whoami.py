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

# Terminal tabs are narrow (often <20 visible chars with several open), and
# the string is emitted inside an escape sequence, so it is capped rather
# than left unbounded.
TAB_TITLE_CAP = 120


def tab_title(cwd: str, title: str) -> str:
    """The terminal tab label for a session: ``<dir> · <title>``.

    The directory comes first deliberately: it is short, stable, and (per
    the user) usually what a tab was manually named anyway, so it survives
    truncation in a narrow tab while the title fills whatever space is
    left. A session with no ``ai-title`` yet — every session for its first
    few turns — falls back to the directory alone rather than showing a
    placeholder.

    Control characters are stripped: this string is emitted inside an OSC
    escape sequence, and a stray ESC/BEL/newline in a title would terminate
    the sequence early and corrupt the terminal.
    """
    base = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd.rstrip("/") else (cwd or "")
    clean = "".join(ch for ch in title if ch.isprintable())
    clean = " ".join(clean.split())
    parts = [p for p in (base, clean) if p]
    return " · ".join(parts)[:TAB_TITLE_CAP]


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
