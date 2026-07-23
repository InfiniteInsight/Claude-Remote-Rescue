import json
import os
import sys

import pytest

from crr import journal


def make_entry(pid=101, **kw):
    return journal.new_entry(
        pid=pid,
        cwd="/home/u/project",
        shell="zsh",
        host="tab",
        boot_id="boot-abc",
        **kw,
    )


def test_round_trip(crr_state):
    entry = make_entry(pid=101)
    path = journal.write_entry(entry)
    assert path == crr_state / "tabs" / "101.json"
    got = journal.read_entry(101)
    assert got is not None
    assert got["v"] == 1
    assert got["pid"] == 101
    assert got["boot_id"] == "boot-abc"
    assert got["cwd"] == "/home/u/project"
    assert got["shell"] == "zsh"
    assert got["host"] == "tab"
    assert got["claude"] is None
    assert got["last_cmd"] is None
    assert got["tmux_session"] is None
    assert got["revived"] == 0
    assert got["updated"]


def test_read_missing_returns_none(crr_state):
    assert journal.read_entry(4242) is None


def test_list_entries_sorted_and_skips_garbage(crr_state):
    for pid in (30, 10, 20):
        journal.write_entry(make_entry(pid=pid))
    tabs = journal.tabs_dir()
    (tabs / "junk.json").write_text("{not json", encoding="utf-8")
    pids = [e["pid"] for e in journal.list_entries()]
    assert pids == [10, 20, 30]


def test_atomic_write_failure_preserves_previous(crr_state):
    entry = make_entry(pid=7)
    journal.write_entry(entry)
    before = journal.read_entry(7)

    def boom(src, dst):
        raise OSError("simulated rename failure")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(journal.os, "replace", boom)
        entry["cwd"] = "/elsewhere"
        with pytest.raises(OSError):
            journal.write_entry(entry)

    # Previous entry intact, no tmp litter left behind.
    after = journal.read_entry(7)
    assert after["cwd"] == before["cwd"] == "/home/u/project"
    leftovers = [p for p in journal.tabs_dir().iterdir() if p.name != "7.json"]
    assert leftovers == []


def test_write_is_single_file_rename(crr_state, monkeypatch):
    """The final path only ever holds complete JSON (tmp+rename)."""
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        # At rename time the temp file already holds the full document.
        with open(src, "r", encoding="utf-8") as fh:
            seen["content"] = json.load(fh)
        real_replace(src, dst)

    monkeypatch.setattr(journal.os, "replace", spy)
    journal.write_entry(make_entry(pid=8))
    assert seen["content"]["pid"] == 8


def test_archive_entry_moves_and_annotates(crr_state):
    journal.write_entry(make_entry(pid=55))
    dest = journal.archive_entry(55, reason="test-reason")
    assert dest is not None
    assert dest.parent == crr_state / "archive"
    assert journal.read_entry(55) is None
    archived = journal.list_archived()
    assert len(archived) == 1
    assert archived[0]["pid"] == 55
    assert archived[0]["archive_reason"] == "test-reason"
    assert archived[0]["archived"]


def test_archive_missing_entry_returns_none(crr_state):
    assert journal.archive_entry(999) is None


def test_delete_entry(crr_state):
    journal.write_entry(make_entry(pid=66))
    assert journal.delete_entry(66) is True
    assert journal.delete_entry(66) is False


def test_state_dir_env_override(crr_state):
    assert journal.state_dir() == crr_state


def test_state_dir_xdg_and_fallback(monkeypatch):
    monkeypatch.delenv("CRR_STATE_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert str(journal.state_dir()) == "/xdg/state/crr"
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert journal.state_dir() == journal.Path.home() / ".local" / "state" / "crr"


def test_state_dir_darwin(monkeypatch):
    monkeypatch.delenv("CRR_STATE_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    expected = journal.Path.home() / "Library" / "Application Support" / "crr"
    assert journal.state_dir() == expected


# ---------------------------------------------------------------------------
# relaunch flag [lesson: flag files]


def test_relaunch_flag_path_under_state_dir(crr_state):
    assert journal.relaunch_flag_path(123) == crr_state / "relaunch" / "123.flag"


def test_take_relaunch_flag_when_absent(crr_state):
    assert journal.take_relaunch_flag(123) is False


def test_write_then_take_relaunch_flag_is_atomic_consume(crr_state):
    path = journal.write_relaunch_flag(123)
    assert path.exists()
    assert journal.take_relaunch_flag(123) is True
    assert not path.exists()
    # A stale/previously-consumed flag never re-fires.
    assert journal.take_relaunch_flag(123) is False


def test_relaunch_flag_is_per_pid(crr_state):
    journal.write_relaunch_flag(1)
    assert journal.take_relaunch_flag(2) is False
    assert journal.take_relaunch_flag(1) is True
