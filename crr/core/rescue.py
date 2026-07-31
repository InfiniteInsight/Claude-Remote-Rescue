"""Rescued-session selection + the per-boot restore-prompt marker.

A "rescued" session is a journal entry from a PREVIOUS boot whose
conversation the reviver parked in a currently-live tmux session: crashed
shell, revived claude, awaiting re-homing. The restore prompt (Phase 3 UX)
offers exactly that set once per boot.

The marker is an opaque per-boot file (like the relaunch flags — no
versioned contract): its existence means "this boot's prompt was already
shown/answered"; markers from other boots are stale and swept on claim.

`claim_prompt` is the atomic once-per-boot claim (Task-8 fix for a
Task-3 review finding): two interactive shells starting together both
used to pass `already_prompted()`'s exists() check before either had
written the marker, so both could prompt and both detmux the same
sessions. `os.open` with O_CREAT|O_EXCL makes "does the marker exist,
and if not create it" atomic — the winner (marker created) claims BEFORE
prompting; the loser (FileExistsError) prompts nothing and exits
silently. The same claim covers the headless-notice outcome too: a
headless host prints its one-line notice only after winning the claim,
so the notice is once-per-boot exactly like the interactive prompt — not
a separate marker-write path that could race independently.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping

_MARKER_PREFIX = "rescue-prompted-"


def rescued_sessions(
    entries: Iterable[Mapping[str, Any]],
    current_boot: str,
    live_tmux: set[str],
) -> list[dict]:
    out = [
        dict(e) for e in entries
        if e.get("claude") is not None
        and e["boot_id"] != current_boot
        and e.get("tmux_session")
        and e["tmux_session"] in live_tmux
    ]
    return sorted(out, key=lambda e: e["pid"])


def marker_path(state_dir: Path | str, boot_id: str) -> Path:
    return Path(state_dir) / f"{_MARKER_PREFIX}{boot_id}"


def already_prompted(state_dir: Path | str, boot_id: str) -> bool:
    """Cheap pre-check: does this boot's marker already exist?

    Not a claim by itself (check-then-act) — callers that are about to
    prompt/notice must use `claim_prompt` for the actual once-per-boot
    guarantee. This stays as a fast path so a hot shell-start doesn't pay
    for a journal/tmux scan once the boot has already been handled.
    """
    return marker_path(state_dir, boot_id).exists()


def claim_prompt(state_dir: Path | str, boot_id: str) -> bool:
    """Atomically claim this boot's once-only prompt/notice slot.

    True: this call created the marker — the caller won and must proceed
    to prompt (or print the headless notice) BEFORE any other visible
    side effect. False: the marker already existed (`FileExistsError`) —
    another shell already claimed it; the caller must stay silent and
    exit, never re-attempt.
    """
    target = marker_path(state_dir, boot_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    for stale in target.parent.glob(f"{_MARKER_PREFIX}*"):
        if stale != target:
            stale.unlink(missing_ok=True)
    return True
