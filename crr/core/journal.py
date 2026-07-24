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
from typing import Any, Mapping, NamedTuple

from crr.core import contracts


def write_json_atomic(target: Path, obj: Any) -> None:
    """Write ``obj`` as JSON to ``target`` via tmp-file + fsync + rename.

    A reader never sees a half-written file, and a crash mid-write leaves
    the previous good file intact. Shared by the journal and archive stores.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=2)
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


def read_json_file(target: Path) -> Any:
    """Parse JSON from ``target``.

    Propagates ``FileNotFoundError`` if the file is missing; raises
    ``contracts.ContractError`` if it exists but is not valid JSON.
    """
    raw = target.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise contracts.ContractError(f"file {target} is not valid JSON: {exc}") from exc


def new_entry(
    *,
    pid: int,
    cwd: str,
    host: str,
    shell: str,
    boot_id: str,
    now: str,
    last_cmd: str = "",
    tmux_session: str | None = None,
    revive_strikes: int = 0,
    claude: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a schema-v1 journal entry, validated before it is returned.

    Centralizes the entry shape in one place (alongside the contract), so
    the register/wrapper code paths cannot drift from the schema. ``now``
    and ``boot_id`` are passed in (the composition root sources them from
    the clock and the boot-identity adapter) to keep this pure/testable.
    """
    entry = {
        "v": contracts.JOURNAL_SCHEMA_VERSION,
        "pid": pid,
        "boot_id": boot_id,
        "cwd": cwd,
        "host": host,
        "shell": shell,
        "claude": dict(claude) if claude is not None else None,
        "last_cmd": last_cmd,
        "tmux_session": tmux_session,
        "revive_strikes": revive_strikes,
        "updated": now,
    }
    contracts.validate_journal_entry(entry)
    return entry


class JournalScan(NamedTuple):
    """Result of scanning the tabs dir.

    ``problems`` exists so corrupt/stale files are surfaced rather than
    silently dropped — a blank dashboard hiding a broken file is exactly
    the kind of laundering the plumb-line principles forbid.
    """

    entries: list[dict[str, Any]]
    problems: list[tuple[str, str]]  # (filename, human-readable reason)


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
        write_json_atomic(self.path_for(entry["pid"]), entry)

    def read(self, pid: int) -> dict[str, Any]:
        """Return the validated entry for ``pid``.

        Raises ``KeyError`` if no file exists, ``contracts.ContractError``
        if the file is unparseable or fails the schema.
        """
        try:
            entry = read_json_file(self.path_for(pid))
        except FileNotFoundError:
            raise KeyError(pid) from None
        contracts.validate_journal_entry(entry)
        return entry

    def remove(self, pid: int) -> None:
        """Delete the entry for ``pid``. Idempotent (missing is a no-op)."""
        try:
            self.path_for(pid).unlink()
        except FileNotFoundError:
            pass

    def scan(self) -> JournalScan:
        """List every ``<pid>.json`` entry, surfacing corrupt files.

        Only files named exactly ``<int>.json`` are considered (temp files
        like ``.<pid>.json.tmp`` and unrelated files are ignored). A file
        that fails to parse or validate becomes a ``problems`` entry rather
        than crashing the scan or vanishing from the result.
        """
        entries: list[dict[str, Any]] = []
        problems: list[tuple[str, str]] = []
        if not self.tabs_dir.is_dir():
            return JournalScan(entries, problems)

        for path in sorted(self.tabs_dir.glob("*.json")):
            if not path.stem.isdigit():
                continue
            try:
                entries.append(self.read(int(path.stem)))
            except (KeyError, contracts.ContractError, OSError) as exc:
                problems.append((path.name, str(exc)))
        return JournalScan(entries, problems)
