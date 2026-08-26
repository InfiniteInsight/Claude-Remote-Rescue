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


# --------------------------------------------------------------------------
# `remote_control` + `waiting_for` (spec 2026-08-09, Phases 1-3, contract
# v12). These replace the five record-counting tests that lived here: the
# card no longer derives the bridge state from `bridge_seen`/`bridge_since`
# and an injected `bridge_stale_records` threshold, so `off`/`ok`/`dropped`
# and the threshold tests went with them. The value is now RESOLVED BY THE
# CALLER — cli reads Claude Code's own per-process state file (filesystem,
# so not core's job) and classifies it through
# `reachability.reachability` — and injected as a sid -> (state,
# waiting_for) map, exactly like `live_tmux_sessions` and the autokick
# overrides below.
# --------------------------------------------------------------------------

def test_the_card_carries_the_injected_reachability():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        reachability_by_sid={sid: ("unreachable", "permission prompt")})
    card = payload["sessions"][0]
    assert card["remote_control"] == "unreachable"
    assert card["waiting_for"] == "permission prompt"


def test_a_session_with_no_reachability_entry_is_unknown():
    # Nothing injected, or the adapter had no state file for it. Absence is
    # not evidence the bridge is down.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions([_entry(42, sid)], FakeBoot(), FakeProbe())
    assert payload["sessions"][0]["remote_control"] == "unknown"
    assert payload["sessions"][0]["waiting_for"] == ""


def test_a_reachability_card_survives_its_own_validator():
    from crr.core import contracts
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        reachability_by_sid={sid: ("reachable", "")})
    contracts.validate_sessions_payload(payload)


# --------------------------------------------------------------------------
# `autokick` card field (spec 2026-08-07, Slice 3): the resolved values are
# injected from the caller (cli reads config.toml + the dashboard's
# SettingsStore, both filesystem — core stays pure), mirroring how
# reachability_by_sid is injected above.
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


def _parked_entry(pid=42, sid="8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
                  name="crr-8a1b2c3d"):
    entry = _entry(pid, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = name
    return entry


# --- attached: which parked sessions the user has already reopened (#32) ---

def test_parked_and_attached_card_is_marked_attached():
    entry = _parked_entry()
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(),
        live_tmux_sessions={"crr-8a1b2c3d"},
        attached_tmux_sessions={"crr-8a1b2c3d"},
    )
    card = payload["sessions"][0]
    assert card["state"] == "parked"
    assert card["attached"] is True


def test_parked_but_detached_card_is_not_attached():
    entry = _parked_entry()
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(),
        live_tmux_sessions={"crr-8a1b2c3d"},
        attached_tmux_sessions=set(),
    )
    card = payload["sessions"][0]
    assert card["state"] == "parked"
    assert card["attached"] is False


def test_unknown_attached_state_never_claims_attached():
    # F16 tri-state: None means "could not determine". Never render a card
    # as attached on the strength of a failed query — fall back to restored.
    entry = _parked_entry()
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(),
        live_tmux_sessions={"crr-8a1b2c3d"},
        attached_tmux_sessions=None,
    )
    assert payload["sessions"][0]["attached"] is False


def test_a_non_parked_card_is_never_attached():
    # A live/crashed card is not "restored", so "attached" is meaningless
    # for it — never set even if some tmux session shares the name.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)  # LIVE (current boot, has tty), host=tmux
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(),
        live_tmux_sessions=set(),
        attached_tmux_sessions={"crr-8a1b2c3d"},
    )
    card = payload["sessions"][0]
    assert card["state"] != "parked"
    assert card["attached"] is False


def test_an_entry_with_no_tmux_session_is_unaffected():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-other"})
    assert payload["sessions"][0]["state"] == "crashed"


def test_a_live_session_in_your_own_terminal_is_never_demoted_to_parked():
    # The protective half of the projection, unchanged by #58: a session the
    # user is running in their OWN terminal must never be pushed into parked
    # just because a tmux session shares its name. (#58 did widen the other
    # half — a LIVE entry whose host IS tmux now reads parked, since a
    # re-keyed revived session classifies live; see
    # test_parked_is_projected_over_a_live_tmux_parked_entry.)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["host"] = "tab"          # a terminal the user owns, not tmux
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-8a1b2c3d"})
    assert payload["sessions"][0]["state"] == "live"


def test_a_parked_card_survives_its_own_validator():
    from crr.core import contracts
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-8a1b2c3d"})
    assert payload["sessions"][0]["state"] == "parked"
    contracts.validate_sessions_payload(payload)   # the half nothing covered


# --- parked covers a re-keyed revived session too (#58) -------------------
#
# Before #58 a revived conversation was journaled under its dead shell pid,
# so PARKED was projected over CRASHED. Now the entry is re-keyed onto the
# live claude, so the same conversation classifies LIVE — and would read as
# a plain "live" card, losing the "this is in tmux, not your terminal"
# signal the Phase 0 spec added.

def test_parked_is_projected_over_a_live_tmux_parked_entry():
    from crr.core import status as st
    entry = {"pid": 2016, "boot_id": "B", "host": "tmux", "tmux_session": "crr-abc",
             "claude": {"session_id": "s"}}

    class Boot:
        def current(self): return "B"

    class Probe:
        def is_alive(self, pid): return True
        def has_controlling_tty(self, pid): return True   # a tmux pane HAS a tty

    assert st._display_state(entry, Boot(), Probe(), {"crr-abc"}) == st.PARKED


def test_a_real_terminal_session_never_reads_parked():
    # Same liveness, but not tmux-hosted: must stay "live".
    from crr.core import status as st
    entry = {"pid": 500, "boot_id": "B", "host": "tab", "tmux_session": None,
             "claude": {"session_id": "s"}}

    class Boot:
        def current(self): return "B"

    class Probe:
        def is_alive(self, pid): return True
        def has_controlling_tty(self, pid): return True

    assert st._display_state(entry, Boot(), Probe(), {"crr-abc"}) == "live"


def test_a_crashed_entry_parked_in_tmux_still_reads_parked():
    # The pre-#58 shape must keep working (entries not yet re-keyed).
    from crr.core import status as st
    entry = {"pid": 1311532, "boot_id": "OLD", "host": "tmux",
             "tmux_session": "crr-abc", "claude": {"session_id": "s"}}

    class Boot:
        def current(self): return "B"

    class Probe:
        def is_alive(self, pid): return False
        def has_controlling_tty(self, pid): return False

    assert st._display_state(entry, Boot(), Probe(), {"crr-abc"}) == st.PARKED


# --- two agents on one conversation (#48) ---------------------------------
#
# `duplicate_group` is not this signal: it fires whenever two entries share
# a sid, which includes the benign shell-plus-its-revived-claude pair. The
# hazard is two entries that each own a LIVE claude — both writing to one
# transcript. Measured on the reporting host, both shapes at once:
#
#   93122659  pid 1687  host=tab  claude_groups=[1160061]   <- a real agent
#             pid 1957  host=tmux claude_groups=[1957]      <- a real agent
#   (and elsewhere) a shell whose claude had died: claude_groups=[]

def _two_entries(sid):
    a, b = _entry(1687, sid), _entry(1957, sid)
    b["host"] = "tmux"
    b["tmux_session"] = "crr-x"
    return [a, b]


def test_two_entries_each_owning_a_claude_are_flagged_as_conflicting():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(_two_entries(sid), FakeBoot(), FakeProbe(),
                                claude_owners={1687: [1160061], 1957: [1957]})
    for card in payload["sessions"]:
        assert card["conflict"] is True
    contracts.validate_sessions_payload(payload)


def test_a_shell_whose_claude_died_is_not_a_conflict():
    # The benign duplicate: the original shell entry lingers beside the
    # revived claude, but only one agent is running.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(_two_entries(sid), FakeBoot(), FakeProbe(),
                                claude_owners={1687: [], 1957: [1957]})
    assert [c["conflict"] for c in payload["sessions"]] == [False, False]


def test_a_lone_session_is_never_a_conflict():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions([_entry(42, sid)], FakeBoot(), FakeProbe(),
                                claude_owners={42: [999]})
    assert payload["sessions"][0]["conflict"] is False


def test_conflict_is_False_not_None_when_the_probe_cannot_answer():
    # An unreadable ps must not become a positive claim that two agents are
    # fighting — that would tell the user to kill something on no evidence.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(_two_entries(sid), FakeBoot(), FakeProbe())
    assert [c["conflict"] for c in payload["sessions"]] == [False, False]


def test_auth_fields_default_to_unknown_and_validate():
    # v15: no auth kwargs passed -> the honest "not resolved" defaults, and
    # the payload must still satisfy the validator.
    payload = assemble_sessions([], FakeBoot(), FakeProbe())
    assert payload["auth_state"] == "unknown"
    assert payload["auth_expires_in_seconds"] is None
    assert payload["auth_reauth_url"] is None
    contracts.validate_sessions_payload(payload)


def test_auth_fields_are_wired_through_and_validate():
    # The cli injects the resolved triple (from crr.core.auth.auth_state);
    # assemble_sessions must pass it through verbatim into the payload, and
    # the result must still round-trip through the validator.
    payload = assemble_sessions(
        [], FakeBoot(), FakeProbe(),
        auth_state="expiring",
        auth_expires_in_seconds=3600,
        auth_reauth_url="https://example.ts.net/reauth",
    )
    assert payload["auth_state"] == "expiring"
    assert payload["auth_expires_in_seconds"] == 3600
    assert payload["auth_reauth_url"] == "https://example.ts.net/reauth"
    contracts.validate_sessions_payload(payload)


def test_owners_of_sid_names_the_processes_a_user_would_choose_between():
    from crr.core.status import owners_of_sid
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    sessions = _two_entries(sid)
    assert owners_of_sid(sessions, {1687: [1160061], 1957: [1957]}, sid) == [1687, 1957]
    # The lingering shell owns nothing: not a process anyone should be
    # offered the chance to kill.
    assert owners_of_sid(sessions, {1687: [], 1957: [1957]}, sid) == [1957]
    assert owners_of_sid(sessions, {}, sid) == []
    assert owners_of_sid(sessions, {1687: [1]}, "another-sid") == []
