"""CLI integration tests (the composition root wired end-to-end).

`config --effective` is pure and runs anywhere. `status --json` exercises
the real adapters (boot identity, process probe) and so is gated to Linux
— the Phase 1 headless target; DESIGN.md gates platform adapter tests
this way.
"""

import json
import os
import platform

import pytest

from crr import cli
from crr.adapters import boot_identity, state_dir
from crr.core import config as cfg
from crr.core import contracts
from crr.core.journal import JournalStore, new_entry


def test_config_effective_lists_every_key_with_origin(capsys):
    rc = cli.main(["config", "--effective"])
    out = capsys.readouterr().out
    assert rc == 0
    for key in cfg.DEFAULTS:
        assert key in out
    assert "(default)" in out


def _live_entry(pid, boot_id):
    return {
        "v": 1,
        "pid": pid,
        "boot_id": boot_id,
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
        "updated": "2026-07-23T00:00:00Z",
    }


@pytest.mark.skipif(platform.system() != "Linux", reason="boot-identity adapter is Linux-only (Phase 1)")
def test_status_json_reports_live_process(tmp_path, monkeypatch, capsys):
    boot_id = boot_identity.LinuxBootIdentity().current()
    store = JournalStore(tmp_path)
    store.write(_live_entry(pid=os.getpid(), boot_id=boot_id))
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    rc = cli.main(["status", "--json"])
    out = capsys.readouterr().out
    assert rc == 0

    payload = json.loads(out)
    contracts.validate_sessions_payload(payload)  # emitted output honors the contract
    (card,) = payload["sessions"]
    assert card["pid"] == os.getpid()
    # This process is alive; with or without a tty it must be live or ghost.
    assert card["state"] in ("live", "ghost")


@pytest.mark.skipif(platform.system() != "Linux", reason="boot-identity adapter is Linux-only (Phase 1)")
def test_status_json_marks_rebooted_session_crashed(tmp_path, monkeypatch, capsys):
    # An entry from a different boot must classify crashed even though its
    # pid (ours) is alive — the recycled-pid guard, end to end.
    store = JournalStore(tmp_path)
    store.write(_live_entry(pid=os.getpid(), boot_id="00000000-0000-4000-8000-000000000000"))
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    cli.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["state"] == "crashed"


# --- shim-facing commands: register / last-cmd / deregister --------------

def _seed(store, pid, cwd="/home/u/p", last_cmd=""):
    store.write(new_entry(
        pid=pid, cwd=cwd, host="tmux", shell="zsh",
        boot_id="b8f3c0de-0000-4000-8000-000000000000",
        now="2026-07-23T00:00:00Z", last_cmd=last_cmd,
    ))


@pytest.mark.skipif(platform.system() != "Linux", reason="register uses the Linux boot adapter")
def test_register_creates_claude_less_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["register", "--pid", "4242", "--cwd", "/home/u/proj",
                   "--shell", "zsh", "--host", "tmux"])
    assert rc == 0
    entry = JournalStore(tmp_path).read(4242)
    assert entry["claude"] is None
    assert entry["cwd"] == "/home/u/proj"
    assert entry["boot_id"] == boot_identity.LinuxBootIdentity().current()


def test_last_cmd_updates_existing_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242, cwd="/old", last_cmd="")
    rc = cli.main(["last-cmd", "--pid", "4242", "--cmd", "claude --resume", "--cwd", "/new"])
    assert rc == 0
    entry = store.read(4242)
    assert entry["last_cmd"] == "claude --resume"
    assert entry["cwd"] == "/new"


def test_last_cmd_on_missing_pid_is_quiet_noop(tmp_path, monkeypatch):
    # Hot-path hook: never disrupt the prompt if the entry is gone.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["last-cmd", "--pid", "999", "--cmd", "x"])
    assert rc == 0
    assert not JournalStore(tmp_path).tabs_dir.joinpath("999.json").exists()


def test_deregister_removes_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    assert cli.main(["deregister", "--pid", "4242"]) == 0
    assert not store.tabs_dir.joinpath("4242.json").exists()
    assert cli.main(["deregister", "--pid", "4242"]) == 0  # second call: no error
