"""Archive store tests (audit P8 — State-first lineage).

The archive preserves revival-bearing entries that leave the active set,
keyed by session_id (NOT pid) so a preserved session survives pid reuse.
Like the journal, writes are atomic and every read/write is
contract-validated; corrupt files are surfaced by scan(), never dropped.
"""

import pytest

from crr.core import contracts
from crr.core.archive import ArchiveStore, is_expired

_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _entry(sid=_SID, pid=42):
    return {
        "v": 1,
        "pid": pid,
        "boot_id": "b8f3c0de-0000-4000-8000-000000000000",
        "cwd": "/home/u/project",
        "host": "tmux",
        "shell": "zsh",
        "claude": {"session_id": sid, "sid_source": "injected", "started": "2026-07-24T00:00:00Z"},
        "last_cmd": "claude",
        "tmux_session": None,
        "revive_strikes": 0,
        "updated": "2026-07-24T00:00:00Z",
    }


def test_archive_then_read_round_trips(tmp_path):
    store = ArchiveStore(tmp_path)
    record = store.archive(_entry(), "superseded-on-register", "2026-07-24T01:00:00Z")
    assert record["reason"] == "superseded-on-register"
    assert record["archived_at"] == "2026-07-24T01:00:00Z"
    assert store.read(_SID) == record


def test_archive_keyed_by_session_id_not_pid(tmp_path):
    store = ArchiveStore(tmp_path)
    store.archive(_entry(sid=_SID, pid=42), "gave-up", "2026-07-24T01:00:00Z")
    assert (tmp_path / "archive" / f"{_SID}.json").is_file()


def test_two_entries_same_pid_different_sid_coexist(tmp_path):
    # The pid-reuse case: same pid, different sessions must both survive.
    store = ArchiveStore(tmp_path)
    a, b = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    store.archive(_entry(sid=a, pid=1000), "superseded-on-register", "2026-07-24T01:00:00Z")
    store.archive(_entry(sid=b, pid=1000), "superseded-on-register", "2026-07-24T02:00:00Z")
    sids = {r["entry"]["claude"]["session_id"] for r in store.scan().records}
    assert sids == {a, b}


def test_archive_rejects_claude_less_entry(tmp_path):
    store = ArchiveStore(tmp_path)
    bad = _entry()
    bad["claude"] = None
    with pytest.raises(contracts.ContractError):
        store.archive(bad, "gave-up", "2026-07-24T01:00:00Z")


def test_read_missing_sid_raises_keyerror(tmp_path):
    store = ArchiveStore(tmp_path)
    with pytest.raises(KeyError):
        store.read("00000000-0000-4000-8000-000000000000")


def test_path_for_rejects_separators(tmp_path):
    store = ArchiveStore(tmp_path)
    with pytest.raises(contracts.ContractError):
        store.path_for("../tabs/99")


def test_remove_is_idempotent(tmp_path):
    store = ArchiveStore(tmp_path)
    store.archive(_entry(), "gave-up", "2026-07-24T01:00:00Z")
    store.remove(_SID)
    assert not (tmp_path / "archive" / f"{_SID}.json").exists()
    store.remove(_SID)  # again: no error


def test_scan_surfaces_corrupt_without_dropping_valid(tmp_path):
    store = ArchiveStore(tmp_path)
    store.archive(_entry(), "gave-up", "2026-07-24T01:00:00Z")
    (store.archive_dir / "broken.json").write_text("{ not json", encoding="utf-8")
    scan = store.scan()
    assert [r["entry"]["claude"]["session_id"] for r in scan.records] == [_SID]
    assert len(scan.problems) == 1


def test_is_expired_respects_retention_window():
    rec = {"archived_at": "2026-07-01T00:00:00+00:00"}
    assert is_expired(rec, "2026-07-20T00:00:00+00:00", retention_days=14) is True   # 19d old
    assert is_expired(rec, "2026-07-10T00:00:00+00:00", retention_days=14) is False  # 9d old


def test_is_expired_keeps_undatable_records():
    assert is_expired({"archived_at": "not-a-date"}, "2026-07-20T00:00:00+00:00", 14) is False
    assert is_expired({}, "2026-07-20T00:00:00+00:00", 14) is False


def test_write_updates_existing_record(tmp_path):
    # Re-archiving the same sid (e.g. strikes advanced) overwrites in place.
    store = ArchiveStore(tmp_path)
    rec = store.archive(_entry(), "superseded-on-register", "2026-07-24T01:00:00Z")
    rec["entry"]["revive_strikes"] = 2
    rec["reason"] = "gave-up"
    store.write(rec)
    got = store.read(_SID)
    assert got["reason"] == "gave-up"
    assert got["entry"]["revive_strikes"] == 2
