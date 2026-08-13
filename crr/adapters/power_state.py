"""Cross-process visibility for the power hold (fix round 1, 2026-08-13).

`crr power` and `crr doctor` run in a separate process from `crr awake`
and have no in-memory handle to what it holds — the hold is a CHILD of
the awake loop, not of them. This module is the I/O half of the fix: an
atomic write/read/clear of one small JSON file the awake loop stamps
after every poll, so a reader elsewhere can see it.

Interpreting the file (dead writer, staleness, malformed shape, "no loop
has ever reported") is PURE policy and lives in
``crr.core.power.interpret`` — this module only moves bytes, reusing the
journal's tmp-file + rename primitive rather than inventing a second one.

Fix round 2 (2026-08-13): absent and unreadable used to both collapse to
``None`` here, on the theory that both mean "nothing trustworthy is
here". Measured wrong — with a hold genuinely active, truncating
``power.json`` mid-write made a reader in a separate process treat the
corrupt file exactly like a file that never existed, and print "holding:
nothing" while the hold was real. They are DIFFERENT claims: a missing
file means no loop has ever written anything (a known nothing); a
present-but-corrupt file might be hiding a real, currently active hold
that a crash or filesystem fault clipped mid-write (an unknown). ``read``
now returns ``crr.core.power.UNREADABLE`` for the latter case, never
``None`` for anything but a genuinely absent file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic
from crr.core.power import UNREADABLE

FILENAME = "power.json"


def path_for(state_dir: Path | str) -> Path:
    return Path(state_dir) / FILENAME


def write(state_dir: Path | str, data: dict[str, Any]) -> None:
    """Stamp the current snapshot. Called by `crr awake` after every poll."""
    write_json_atomic(path_for(state_dir), data)


def read(state_dir: Path | str) -> dict[str, Any] | Any:
    """The raw snapshot: a parsed dict, ``None`` if no file exists at all,
    or ``crr.core.power.UNREADABLE`` if a file exists but could not be
    parsed as JSON, or parsed to something other than a JSON object.

    ``None`` and ``UNREADABLE`` are deliberately NOT interchangeable —
    see the module docstring. ``crr.core.power.interpret`` is the one
    place licensed to turn either into a specific, honest message; this
    function only reports which of the three states it found.
    """
    try:
        result = read_json_file(path_for(state_dir))
    except FileNotFoundError:
        return None
    except (contracts.ContractError, OSError):
        return UNREADABLE
    return result if isinstance(result, dict) else UNREADABLE


def clear(state_dir: Path | str) -> None:
    """Remove the snapshot. Idempotent — a clean stop must leave no stale claim.

    Called from the SAME ``finally`` that releases the hold in `crr
    awake` (a clean stop or `--once`), and from `crr power --release`
    (fix round 2) so a crashed loop's stale claim doesn't survive the
    user's own recovery action.
    """
    try:
        path_for(state_dir).unlink()
    except FileNotFoundError:
        pass
