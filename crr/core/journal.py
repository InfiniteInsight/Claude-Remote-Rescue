"""Journal store — one JSON file per session (schema v1).

State lives at ``state_dir/tabs/<pid>.json``. The store is pure core: it
receives ``state_dir`` from the caller (the platform state-dir adapter,
wired in crr.cli, computes the real location — ``$XDG_STATE_HOME/crr`` on
Linux, ``~/Library/Application Support/crr`` on macOS). Keeping the path
injected is what lets the store be unit-tested on a tmpdir.

Every write and read is validated against ``contracts.validate_journal_entry``
so a corrupt or stale-schema file is caught rather than silently trusted.
Writes are tmp-file + atomic rename: a reader never sees a half-written
file, and a crash mid-write leaves the previous good file intact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from crr.core import contracts


class JournalStore:
    def __init__(self, state_dir: Path | str) -> None:
        self._state_dir = Path(state_dir)

    @property
    def tabs_dir(self) -> Path:
        return self._state_dir / "tabs"

    def path_for(self, pid: int) -> Path:
        return self.tabs_dir / f"{pid}.json"

    def write(self, entry: Mapping[str, Any]) -> None:
        """Validate ``entry`` then persist it atomically.

        Validation happens BEFORE any filesystem effect, so a rejected
        entry never creates, clobbers, or leaves a temp file behind.
        """
        contracts.validate_journal_entry(entry)
        pid = entry["pid"]
        self.tabs_dir.mkdir(parents=True, exist_ok=True)
        target = self.path_for(pid)
        tmp = target.with_name(f".{pid}.json.tmp")
        data = json.dumps(entry, ensure_ascii=False, indent=2)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)  # atomic on POSIX and Windows
        finally:
            # If the rename succeeded, tmp is gone; if it failed, clean up.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def read(self, pid: int) -> dict[str, Any]:
        """Return the validated entry for ``pid``.

        Raises ``KeyError`` if no file exists, ``contracts.ContractError``
        if the file is unparseable or fails the schema.
        """
        target = self.path_for(pid)
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise KeyError(pid) from None
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise contracts.ContractError(
                f"journal file {target} is not valid JSON: {exc}"
            ) from exc
        contracts.validate_journal_entry(entry)
        return entry

    def remove(self, pid: int) -> None:
        """Delete the entry for ``pid``. Idempotent (missing is a no-op)."""
        try:
            self.path_for(pid).unlink()
        except FileNotFoundError:
            pass
