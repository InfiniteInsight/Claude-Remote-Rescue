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
        "revive_strikes": 0,
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

    def controlling_ttys(self, pids):
        return set(pids) if self._tty else set()


def test_tty_probe_is_batched_once_across_all_cards():
    # DESIGN 'snap jq' perf: the tty check is one batched query for all pids,
    # never one ps per card. Prove it: controlling_ttys is called exactly
    # once, and the per-pid has_controlling_tty is never used on this path.
    class RecordingProbe:
        def __init__(self):
            self.batch_calls = 0
            self.per_pid_calls = 0
            self.seen = None

        def is_alive(self, pid):
            return True

        def has_controlling_tty(self, pid):
            self.per_pid_calls += 1
            return True

        def controlling_ttys(self, pids):
            self.batch_calls += 1
            self.seen = list(pids)
            return set(pids)  # all have a tty -> all live

    entries = [_entry(10, "s-a"), _entry(11, "s-b"), _entry(12, "s-c")]
    probe = RecordingProbe()
    payload = assemble_sessions(entries, FakeBoot(), probe)
    assert probe.batch_calls == 1
    assert probe.per_pid_calls == 0
    assert sorted(probe.seen) == [10, 11, 12]
    assert all(c["state"] == "live" for c in payload["sessions"])


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


def test_tail_facts_extractor_is_used_for_prompt_and_model():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)],
        FakeBoot(),
        FakeProbe(),
        tail_facts=lambda entry: {"last_prompt": f"prompt-for-{entry['pid']}", "model": "claude-opus-5"},
    )
    card = payload["sessions"][0]
    assert card["last_prompt"] == "prompt-for-42"
    assert card["model"] == "claude-opus-5"


def test_tail_facts_default_to_empty_strings():
    # No extractor wired: honest empty strings, not a fabricated prompt/model.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions([_entry(42, sid)], FakeBoot(), FakeProbe())
    card = payload["sessions"][0]
    assert card["last_prompt"] == ""
    assert card["model"] == ""


def test_card_carries_tmux_session():
    parked = _entry(1, "deadbeefcafe0001")
    parked["tmux_session"] = "crr-deadbeef"
    plain = _entry(2, "deadbeefcafe0002")
    payload = assemble_sessions([parked, plain], FakeBoot(), FakeProbe())
    by_pid = {c["pid"]: c for c in payload["sessions"]}
    assert by_pid[1]["tmux_session"] == "crr-deadbeef"
    assert by_pid[2]["tmux_session"] is None


def test_claude_less_shells_are_not_cards():
    # A registered shell with no claude session yet (claude=None) is not a
    # rescuable "session" and must not appear as a card.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entries = [_entry(1, sid), _entry(2, sid)]
    entries[1]["claude"] = None
    payload = assemble_sessions(entries, FakeBoot(), FakeProbe())
    contracts.validate_sessions_payload(payload)
    assert [c["pid"] for c in payload["sessions"]] == [1]
