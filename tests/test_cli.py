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
from crr.core.journal import JournalStore


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
