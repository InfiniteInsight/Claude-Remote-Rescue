"""Rescued-session selection + the per-boot restore-prompt marker.

A "rescued" session is a journal entry from a PREVIOUS boot whose
conversation the reviver parked in a currently-live tmux session: crashed
shell, revived claude, awaiting re-homing. The restore prompt (Phase 3 UX)
offers exactly that set once per boot.

The marker is an opaque per-boot file (like the relaunch flags — no
versioned contract): its existence means "this boot's prompt was already
shown/answered"; markers from other boots are stale and swept on write.
"""

from __future__ import annotations

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
    return marker_path(state_dir, boot_id).exists()


def mark_prompted(state_dir: Path | str, boot_id: str) -> None:
    target = marker_path(state_dir, boot_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    for stale in target.parent.glob(f"{_MARKER_PREFIX}*"):
        if stale != target:
            stale.unlink(missing_ok=True)
    target.touch()
