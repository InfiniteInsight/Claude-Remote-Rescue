"""Dashboard-managed discovery exclusions (the admin section's store).

Discovery skips transcripts under directories the user doesn't consider
their own conversations (claude-mem's observer sessions, scratch dirs, …).
That list has TWO owners, deliberately:

- ``discover_exclude_dirs`` in ``config.toml`` — the user's hand-owned
  baseline, edited in their editor, never touched by the web.
- this store (``<state_dir>/exclusions.json``) — additions made from the
  dashboard.

They stay separate because the dashboard cannot safely write TOML: the
stdlib ships a reader (``tomllib``) but no writer, this project forbids
runtime dependencies, and a hand-rolled serializer would silently destroy
the comments and formatting in a file the user maintains by hand. JSON is
stdlib, round-trips losslessly, and keeps the blast radius of a web write
to a file crr alone owns.

Pure core file I/O, consistent with journal.py/flags.py (core owns the
state-dir filesystem).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic

# Bounds on what a POST may store: a malformed or hostile request must not
# be able to write an enormous file, and a pattern longer than any real path
# component is a mistake rather than an intent.
MAX_ENTRIES = 100
MAX_ENTRY_LEN = 200

FILENAME = "exclusions.json"


class ExclusionError(ValueError):
    """A rejected exclusions payload (shape, type, or bounds)."""


def normalize(dirs: Any) -> list[str]:
    """Validate and clean an exclusions list, or raise ``ExclusionError``.

    Strips whitespace, drops blank entries (``is_excluded`` treats a blank
    pattern as matching nothing — keeping them out of the stored list means
    the file never accumulates no-ops), and de-duplicates while preserving
    the order the user entered. Rejects anything that isn't a list of
    strings, and enforces the count/length bounds above.
    """
    if not isinstance(dirs, list):
        raise ExclusionError("exclusions must be a list of strings")
    if len(dirs) > MAX_ENTRIES:
        raise ExclusionError(f"too many exclusions (max {MAX_ENTRIES})")
    out: list[str] = []
    for entry in dirs:
        if not isinstance(entry, str):
            raise ExclusionError("every exclusion must be a string")
        cleaned = entry.strip()
        if not cleaned:
            continue
        if len(cleaned) > MAX_ENTRY_LEN:
            raise ExclusionError(f"exclusion too long (max {MAX_ENTRY_LEN} chars)")
        if cleaned not in out:
            out.append(cleaned)
    return out


def effective(configured: list[str], managed: list[str]) -> list[str]:
    """The list discovery actually filters on: baseline first, then additions.

    De-duplicated, so a managed entry that repeats a config one is listed
    once. Order is only cosmetic (``is_excluded`` is an any-match), but
    config-first keeps the display honest about where each came from.
    """
    out: list[str] = []
    for entry in list(configured) + list(managed):
        if isinstance(entry, str) and entry.strip() and entry not in out:
            out.append(entry)
    return out


class ExclusionStore:
    """Read/write the dashboard-managed exclusions file."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / FILENAME

    def read(self) -> list[str]:
        """The managed exclusions, or ``[]``.

        A missing OR corrupt file degrades to an empty list rather than
        raising: this is consulted on every discovery pass, and a bad file
        must not take the panel (or `crr discover`) down with it.

        A file stamped with a version this build does not understand (#36)
        also degrades to ``[]`` rather than being partially read. Degrading
        is safe HERE specifically because the failure direction is benign:
        forgetting an exclusion shows the user extra rows in a panel, it
        does not act on anything. The sibling stores (settings, kicks) gate
        destructive work and so surface ``is_degraded()`` instead.
        """
        try:
            data = read_json_file(self._path)
        except (OSError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        if not contracts.store_version_ok(data, contracts.EXCLUSIONS_STORE_VERSION):
            return []
        try:
            return normalize(data.get("dirs", []))
        except ExclusionError:
            return []

    def write(self, dirs: Any) -> list[str]:
        """Validate, store atomically, and return the normalized list."""
        cleaned = normalize(dirs)
        write_json_atomic(
            self._path, {"v": contracts.EXCLUSIONS_STORE_VERSION, "dirs": cleaned}
        )
        return cleaned
