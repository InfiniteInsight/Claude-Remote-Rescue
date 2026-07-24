"""Archive store — preserved revival-bearing entries (audit P8 lineage).

When a claude-bearing entry leaves the active set — clobbered by a
register on a reused pid, or abandoned by the reviver's give-up guard — it
is preserved here rather than lost. Records are keyed by ``session_id``
(NOT pid), so two sessions that reused the same pid both survive.

Same discipline as the journal store: atomic writes, contract-validated
reads and writes, corrupt files surfaced by ``scan()`` rather than dropped.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic


class ArchiveScan(NamedTuple):
    records: list[dict[str, Any]]
    problems: list[tuple[str, str]]  # (filename, human-readable reason)


def is_expired(record: Mapping[str, Any], now_iso: str, retention_days: int) -> bool:
    """True if ``record`` is older than the retention window.

    Unparseable timestamps return False — gc keeps a record it can't date
    rather than deleting on ambiguity (lineage is not thrown away on a
    doubt).
    """
    try:
        archived = datetime.fromisoformat(record["archived_at"])
        now = datetime.fromisoformat(now_iso)
    except (ValueError, TypeError, KeyError):
        return False
    return (now - archived) > timedelta(days=retention_days)


class ArchiveStore:
    def __init__(self, state_dir: Path | str) -> None:
        self._state_dir = Path(state_dir)

    @property
    def archive_dir(self) -> Path:
        return self._state_dir / "archive"

    def path_for(self, session_id: str) -> Path:
        return self.archive_dir / f"{session_id}.json"

    def archive(self, entry: Mapping[str, Any], reason: str, now: str) -> dict[str, Any]:
        """Preserve ``entry`` with lineage; return the written record."""
        record = {
            "v": contracts.ARCHIVE_CONTRACT_VERSION,
            "reason": reason,
            "archived_at": now,
            "entry": dict(entry),
        }
        self.write(record)
        return record

    def write(self, record: Mapping[str, Any]) -> None:
        """Validate ``record`` then persist it atomically (keyed by sid)."""
        contracts.validate_archive_record(record)
        sid = record["entry"]["claude"]["session_id"]
        write_json_atomic(self.path_for(sid), record)

    def read(self, session_id: str) -> dict[str, Any]:
        """Return the validated record for ``session_id`` (KeyError if absent)."""
        try:
            record = read_json_file(self.path_for(session_id))
        except FileNotFoundError:
            raise KeyError(session_id) from None
        contracts.validate_archive_record(record)
        return record

    def remove(self, session_id: str) -> None:
        """Delete the record for ``session_id``. Idempotent."""
        try:
            self.path_for(session_id).unlink()
        except FileNotFoundError:
            pass

    def scan(self) -> ArchiveScan:
        """List every archive record, surfacing corrupt files as problems."""
        records: list[dict[str, Any]] = []
        problems: list[tuple[str, str]] = []
        if not self.archive_dir.is_dir():
            return ArchiveScan(records, problems)

        for path in sorted(self.archive_dir.glob("*.json")):
            try:
                record = read_json_file(path)
                contracts.validate_archive_record(record)
                records.append(record)
            except (contracts.ContractError, OSError) as exc:
                problems.append((path.name, str(exc)))
        return ArchiveScan(records, problems)
