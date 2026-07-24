"""Status assembler tests — entries + classifier -> /api/sessions payload.

Pure core with fake ports. The assembled payload must satisfy the P7
contract validator (so the server can't drift from it), duplicate groups
must be detected across entries, and each card must carry sid_source so
the dashboard can weight guessed claims (audit P3).
"""

from crr.core import contracts
from crr.core.status import assemble_sessions


_BOOT = "b8f3c0de-0000-4000-8000-000000000000"


def _entry(pid, session_id, sid_source="injected"):
    return {
        "v": 1,
        "pid": pid,
        "boot_id": _BOOT,
        "cwd": "/home/u/project",
        "host": "tmux",
        "shell": "zsh",
        "claude": {
            "session_id": session_id,
            "sid_source": sid_source,
            "started": "2026-07-23T00:00:00Z",
        },
        "last_cmd": "claude",
        "tmux_session": None,
        "updated": "2026-07-23T00:00:00Z",
    }


class FakeBoot:
    def current(self):
        return _BOOT


class FakeProbe:
    def __init__(self, alive=True, tty=True):
        self._alive, self._tty = alive, tty

    def is_alive(self, pid):
        return self._alive

    def has_controlling_tty(self, pid):
        return self._tty


def test_empty_entries_produce_empty_valid_payload():
    payload = assemble_sessions([], FakeBoot(), FakeProbe())
    assert payload["contract"] == contracts.SESSIONS_CONTRACT_VERSION
    assert payload["sessions"] == []
    contracts.validate_sessions_payload(payload)


def test_single_entry_card_fields():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid, sid_source="guessed")], FakeBoot(), FakeProbe(alive=True, tty=False)
    )
    contracts.validate_sessions_payload(payload)
    (card,) = payload["sessions"]
    assert card["pid"] == 42
    assert card["state"] == "ghost"  # alive, no tty
    assert card["session_id"] == sid
    assert card["sid8"] == "8a1b2c3d"
    assert card["sid_source"] == "guessed"  # provenance survives to the card
    assert card["duplicate_group"] is None


def test_duplicate_session_ids_share_a_group():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(1, sid), _entry(2, sid)], FakeBoot(), FakeProbe()
    )
    contracts.validate_sessions_payload(payload)
    groups = {c["pid"]: c["duplicate_group"] for c in payload["sessions"]}
    assert groups[1] is not None
    assert groups[1] == groups[2]


def test_distinct_session_ids_are_not_grouped():
    payload = assemble_sessions(
        [
            _entry(1, "11111111-1111-4111-8111-111111111111"),
            _entry(2, "22222222-2222-4222-8222-222222222222"),
        ],
        FakeBoot(),
        FakeProbe(),
    )
    for card in payload["sessions"]:
        assert card["duplicate_group"] is None


def test_last_prompt_extractor_is_used():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)],
        FakeBoot(),
        FakeProbe(),
        last_prompt=lambda entry: f"prompt-for-{entry['pid']}",
    )
    assert payload["sessions"][0]["last_prompt"] == "prompt-for-42"


def test_last_prompt_defaults_to_empty_string():
    # No extractor wired yet (transcript reading is a later increment):
    # honest empty string, not a fabricated prompt.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions([_entry(42, sid)], FakeBoot(), FakeProbe())
    assert payload["sessions"][0]["last_prompt"] == ""
