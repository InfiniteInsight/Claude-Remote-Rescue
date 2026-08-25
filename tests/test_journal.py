"""Journal store tests (schema v1, one JSON file per session).

The store is pure core: it takes an injected state dir (the platform
state-dir adapter computes the real one and lives in crr.adapters), so
these tests run on a tmpdir with no platform coupling. Writes are
tmp-file+rename (atomic); every read and write is contract-validated so a
corrupt or stale-schema file is caught, never silently trusted.
"""

import json

import pytest

from crr.core import contracts
from crr.core.journal import JournalStore, new_entry


def _entry(pid=12345):
    return {
        "v": 1,
        "pid": pid,
        "boot_id": "b8f3c0de-0000-4000-8000-000000000000",
        "cwd": "/home/u/project",
        "host": "tmux",
        "shell": "zsh",
        "claude": {
            "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "sid_source": "injected",
            "started": "2026-07-23T00:00:00Z",
        },
        "last_cmd": "claude",
        "tmux_session": None,
        "revive_strikes": 0,
        "updated": "2026-07-23T00:00:00Z",
    }


def test_write_then_read_round_trips(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(pid=42))
    assert store.read(42) == _entry(pid=42)


def test_entry_lives_under_tabs_dir(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(pid=42))
    assert (tmp_path / "tabs" / "42.json").is_file()


def test_write_rejects_invalid_entry(tmp_path):
    store = JournalStore(tmp_path)
    bad = _entry()
    del bad["boot_id"]
    with pytest.raises(contracts.ContractError):
        store.write(bad)


def test_write_of_invalid_entry_leaves_no_file(tmp_path):
    # A rejected write must not create or clobber a file, nor leave a temp.
    store = JournalStore(tmp_path)
    bad = _entry(pid=7)
    bad["host"] = "carrier-pigeon"
    with pytest.raises(contracts.ContractError):
        store.write(bad)
    assert list((tmp_path / "tabs").glob("*")) == [] or not (tmp_path / "tabs").exists()


def test_write_leaves_no_temp_files(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(pid=42))
    leftovers = [p.name for p in (tmp_path / "tabs").iterdir() if p.name != "42.json"]
    assert leftovers == []


def test_read_missing_pid_raises_keyerror(tmp_path):
    store = JournalStore(tmp_path)
    with pytest.raises(KeyError):
        store.read(999)


def test_read_rejects_corrupt_file(tmp_path):
    store = JournalStore(tmp_path)
    tabs = tmp_path / "tabs"
    tabs.mkdir(parents=True)
    (tabs / "42.json").write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(contracts.ContractError):
        store.read(42)


def test_read_rejects_stale_schema_file(tmp_path):
    # A file from a future/older schema version must be caught on read.
    store = JournalStore(tmp_path)
    tabs = tmp_path / "tabs"
    tabs.mkdir(parents=True)
    stale = _entry(pid=42)
    stale["v"] = 999
    (tabs / "42.json").write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(contracts.ContractError):
        store.read(42)


def test_remove_is_idempotent(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(pid=42))
    store.remove(42)
    assert not (tmp_path / "tabs" / "42.json").exists()
    store.remove(42)  # second remove must not raise


def test_overwrite_replaces_entry(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(pid=42))
    updated = _entry(pid=42)
    updated["cwd"] = "/home/u/elsewhere"
    store.write(updated)
    assert store.read(42)["cwd"] == "/home/u/elsewhere"


# --- scan(): list all entries, surfacing (never hiding) corrupt files ----

def test_scan_empty_when_no_tabs_dir(tmp_path):
    store = JournalStore(tmp_path)
    scan = store.scan()
    assert scan.entries == []
    assert scan.problems == []


def test_scan_returns_all_valid_entries(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(pid=1))
    store.write(_entry(pid=2))
    scan = store.scan()
    assert sorted(e["pid"] for e in scan.entries) == [1, 2]
    assert scan.problems == []


def test_scan_reports_corrupt_file_without_dropping_valid_ones(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(pid=1))
    (store.tabs_dir / "2.json").write_text("{ not json", encoding="utf-8")
    scan = store.scan()
    assert [e["pid"] for e in scan.entries] == [1]
    assert len(scan.problems) == 1
    problem_name, problem_msg = scan.problems[0]
    assert "2.json" in problem_name
    assert problem_msg  # a non-empty explanation, not a silent drop


def test_scan_ignores_non_json_and_temp_files(tmp_path):
    store = JournalStore(tmp_path)
    store.write(_entry(pid=1))
    (store.tabs_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
    (store.tabs_dir / ".3.json.tmp").write_text("{}", encoding="utf-8")
    scan = store.scan()
    assert [e["pid"] for e in scan.entries] == [1]
    assert scan.problems == []


# --- new_entry(): build a valid schema-v1 entry --------------------------

def test_new_entry_is_contract_valid_and_claude_less_by_default():
    entry = new_entry(
        pid=42,
        cwd="/home/u/p",
        host="tmux",
        shell="zsh",
        boot_id="b8f3c0de-0000-4000-8000-000000000000",
        now="2026-07-23T00:00:00Z",
    )
    contracts.validate_journal_entry(entry)  # must not raise
    assert entry["v"] == contracts.JOURNAL_SCHEMA_VERSION
    assert entry["pid"] == 42
    assert entry["updated"] == "2026-07-23T00:00:00Z"
    assert entry["claude"] is None
    assert entry["last_cmd"] == ""
    assert entry["tmux_session"] is None


def test_new_entry_carries_claude_when_given():
    claude = {
        "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "sid_source": "injected",
        "started": "2026-07-23T00:00:00Z",
        "skip_permissions": False,
    }
    entry = new_entry(
        pid=42, cwd="/x", host="ssh", shell="bash",
        boot_id="b", now="2026-07-23T00:00:00Z", claude=claude,
    )
    contracts.validate_journal_entry(entry)
    assert entry["claude"] == claude
