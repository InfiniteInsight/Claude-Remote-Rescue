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
        tail_facts=lambda entry: {
            "last_prompt": f"prompt-for-{entry['pid']}",
            "last_reply": f"reply-for-{entry['pid']}",
            "title": f"title-for-{entry['pid']}",
            "slug": "some-memorable-slug",
            "model": "claude-opus-5",
            "last_active": "2026-08-01T00:00:00Z",
            "transcript_bytes": 0,
            "bridge_seen": False,
            "bridge_since": 0,
        },
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
    assert card["last_active"] == ""


def test_card_carries_last_active_from_tail_facts():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)],
        FakeBoot(),
        FakeProbe(),
        tail_facts=lambda entry: {
            "last_prompt": "",
            "last_reply": "",
            "title": "",
            "slug": "",
            "model": "",
            "last_active": "2026-08-01T12:34:56Z",
            "transcript_bytes": 0,
            "bridge_seen": False,
            "bridge_since": 0,
        },
    )
    card = payload["sessions"][0]
    assert card["last_active"] == "2026-08-01T12:34:56Z"


def test_card_context_pressure_is_unknown_with_no_transcript():
    # #39: the default extractor reports model="" (nothing extracted), so
    # there is no confirmed context window to divide by. Previously this
    # read "ok" — a reassuring claim derived from a fabricated 200K.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions([_entry(42, sid)], FakeBoot(), FakeProbe())
    assert payload["sessions"][0]["context_pressure"] == "unknown"


def test_card_context_pressure_will_compact_when_over_window():
    # haiku-4.5's confirmed window is 200_000 tokens; estimate_tokens is
    # bytes // 4, so 200_000 * 4 bytes clears the compact fraction
    # (default 1.0). A MAPPED model is required since #39 — an unmapped one
    # now yields "unknown" rather than borrowing DEFAULT_WINDOW.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)],
        FakeBoot(),
        FakeProbe(),
        tail_facts=lambda entry: {
            "last_prompt": "",
            "last_reply": "",
            "title": "",
            "slug": "",
            "model": "claude-haiku-4-5-20251001",
            "last_active": "",
            "transcript_bytes": 200_000 * 4,
            "bridge_seen": False,
            "bridge_since": 0,
        },
    )
    assert payload["sessions"][0]["context_pressure"] == "will-compact"


def test_card_context_pressure_thresholds_are_configurable():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)],
        FakeBoot(),
        FakeProbe(),
        tail_facts=lambda entry: {
            "last_prompt": "",
            "last_reply": "",
            "title": "",
            "slug": "",
            "model": "claude-haiku-4-5-20251001",
            "last_active": "",
            "transcript_bytes": 100_000 * 4,  # fraction 0.5 of haiku-4.5's 200_000 window
            "bridge_seen": False,
            "bridge_since": 0,
        },
        context_tight_fraction=0.4,
        context_compact_fraction=0.9,
    )
    assert payload["sessions"][0]["context_pressure"] == "tight"


def test_card_carries_tmux_session():
    parked = _entry(1, "deadbeefcafe0001")
    parked["tmux_session"] = "crr-deadbeef"
    plain = _entry(2, "deadbeefcafe0002")
    payload = assemble_sessions([parked, plain], FakeBoot(), FakeProbe())
    by_pid = {c["pid"]: c for c in payload["sessions"]}
    assert by_pid[1]["tmux_session"] == "crr-deadbeef"
    assert by_pid[2]["tmux_session"] is None


def _facts(**over):
    base = {
        "last_prompt": "", "last_reply": "", "title": "", "slug": "",
        "model": "", "last_active": "", "transcript_bytes": 0,
        "bridge_seen": False, "bridge_since": 0,
    }
    base.update(over)
    return base


def test_card_remote_control_off_when_no_marker_ever_seen():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        tail_facts=lambda entry: _facts(bridge_seen=False, bridge_since=0),
    )
    assert payload["sessions"][0]["remote_control"] == "off"


def test_card_remote_control_ok_within_threshold():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        tail_facts=lambda entry: _facts(bridge_seen=True, bridge_since=10),
        bridge_stale_records=150,
    )
    assert payload["sessions"][0]["remote_control"] == "ok"


def test_card_remote_control_dropped_past_threshold():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        tail_facts=lambda entry: _facts(bridge_seen=True, bridge_since=151),
        bridge_stale_records=150,
    )
    assert payload["sessions"][0]["remote_control"] == "dropped"


def test_card_remote_control_threshold_is_configurable():
    # Same bridge_since, a tighter threshold flips ok -> dropped: the
    # caller-injected value is actually honoured, not a hardcoded 150.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        tail_facts=lambda entry: _facts(bridge_seen=True, bridge_since=20),
        bridge_stale_records=10,
    )
    assert payload["sessions"][0]["remote_control"] == "dropped"


def test_card_remote_control_is_unknown_with_no_tail_facts_wired():
    # #33: no extractor means no transcript was read, so bridge_seen is None
    # and the card says "unknown". It used to say "off" — asserting Remote
    # Control was never enabled, on the strength of never having looked.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions([_entry(42, sid)], FakeBoot(), FakeProbe())
    assert payload["sessions"][0]["remote_control"] == "unknown"


def test_card_remote_control_is_off_only_when_the_whole_transcript_was_read():
    # The one case that licenses the positive claim: the adapter reports
    # bridge_seen=False, meaning it walked to the start of the transcript
    # inside its scan window and found no marker.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        tail_facts=lambda entry: {
            "last_prompt": "", "last_reply": "", "title": "", "slug": "",
            "model": "", "last_active": "", "transcript_bytes": 0,
            "bridge_seen": False, "bridge_since": 0,
        },
    )
    assert payload["sessions"][0]["remote_control"] == "off"


# --------------------------------------------------------------------------
# `autokick` card field (spec 2026-08-07, Slice 3): the resolved values are
# injected from the caller (cli reads config.toml + the dashboard's
# SettingsStore, both filesystem — core stays pure), mirroring how
# bridge_stale_records is injected above.
# --------------------------------------------------------------------------

def test_card_autokick_on_by_default_with_nothing_injected():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions([_entry(42, sid)], FakeBoot(), FakeProbe())
    assert payload["sessions"][0]["autokick"] == "on"


def test_card_autokick_off_when_this_session_opted_out():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        autokick_session_overrides={sid: False},
    )
    assert payload["sessions"][0]["autokick"] == "off"


def test_card_autokick_unrelated_session_override_does_not_leak():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    other_sid = "11112222-3333-4444-5555-666677778888"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        autokick_session_overrides={other_sid: False},
    )
    assert payload["sessions"][0]["autokick"] == "on"


def test_card_autokick_global_off_via_dashboard_override():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        autokick_global_override=False,
        autokick_session_overrides={sid: True},
    )
    # global-off is a hard switch: even a session that opted IN shows the
    # global reason, never a lying "on" it cannot honour.
    assert payload["sessions"][0]["autokick"] == "global-off"


def test_card_autokick_global_off_via_config_default():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        autokick_config_default=False,
    )
    assert payload["sessions"][0]["autokick"] == "global-off"


def test_claude_less_shells_are_not_cards():
    # A registered shell with no claude session yet (claude=None) is not a
    # rescuable "session" and must not appear as a card.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entries = [_entry(1, sid), _entry(2, sid)]
    entries[1]["claude"] = None
    payload = assemble_sessions(entries, FakeBoot(), FakeProbe())
    contracts.validate_sessions_payload(payload)
    assert [c["pid"] for c in payload["sessions"]] == [1]


def test_a_crashed_entry_parked_in_a_live_tmux_session_reads_parked():
    # After a reboot the reviver restores conversations into detached tmux.
    # The journal keeps the pre-reboot pid and boot_id, so classify() says
    # CRASHED — correct for "may I act on this pid", wrong for the card.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-8a1b2c3d"})
    assert payload["sessions"][0]["state"] == "parked"


def test_a_crashed_entry_whose_tmux_session_is_gone_stays_crashed():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions=set())
    assert payload["sessions"][0]["state"] == "crashed"


def test_unknown_tmux_state_never_promotes_to_parked():
    # F16 tri-state: None means "could not determine". Promoting on it
    # would assert a session is running on the strength of a failed query.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions=None)
    assert payload["sessions"][0]["state"] == "crashed"


def test_an_entry_with_no_tmux_session_is_unaffected():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-other"})
    assert payload["sessions"][0]["state"] == "crashed"


def test_a_live_session_is_never_demoted_to_parked():
    # The projection is one-directional: tmux liveness may only rescue an
    # entry from a wrong `crashed`, never push one into parked.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-8a1b2c3d"})
    assert payload["sessions"][0]["state"] == "live"
