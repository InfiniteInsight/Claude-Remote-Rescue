"""Cross-process visibility for the power hold (fix round 1, 2026-08-13).

`crr power` and `crr doctor` run in a separate process from `crr awake`
and have no in-memory handle to what it holds — the hold is a CHILD of
the awake loop, not of them. This module is the I/O half of the fix: an
atomic write/read/clear of one small JSON file the awake loop stamps
after every poll, so a reader elsewhere can see it.

Interpreting the file (dead writer, staleness, "no loop has ever
reported") is PURE policy and lives in ``crr.core.power.interpret`` — this
module only moves bytes, reusing the journal's tmp-file + rename primitive
rather than inventing a second one. Malformed or missing JSON both become
``None`` here, never a partial dict papered over with defaults: `interpret`
is the one place allowed to turn a `None` into an honest message, and it
needs a clean signal to do that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic

FILENAME = "power.json"


def path_for(state_dir: Path | str) -> Path:
    return Path(state_dir) / FILENAME


def write(state_dir: Path | str, data: dict[str, Any]) -> None:
    """Stamp the current snapshot. Called by `crr awake` after every poll."""
    write_json_atomic(path_for(state_dir), data)


def read(state_dir: Path | str) -> dict[str, Any] | None:
    """The raw snapshot dict, or ``None`` if absent/unreadable.

    A missing file and a corrupt one are the SAME signal to a reader —
    "nothing trustworthy is here" — so both collapse to ``None`` rather
    than one of them raising. ``crr.core.power.interpret`` is what turns
    that ``None`` into a specific, honest message; this function is not
    licensed to guess which reason applies.
    """
    try:
        result = read_json_file(path_for(state_dir))
    except (FileNotFoundError, contracts.ContractError, OSError):
        return None
    return result if isinstance(result, dict) else None


def clear(state_dir: Path | str) -> None:
    """Remove the snapshot. Idempotent — a clean stop must leave no stale claim.

    Called from the SAME ``finally`` that releases the hold in `crr
    awake`, so a normal stop (or `--once`) never leaves a state file
    describing a hold that no longer exists.
    """
    try:
        path_for(state_dir).unlink()
    except FileNotFoundError:
        pass
