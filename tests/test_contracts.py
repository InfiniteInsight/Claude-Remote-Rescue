"""Contract-validator tests (audit P7 — Contracted outputs).

These are the scaffold's first test fixtures per ROADMAP Phase 0. They
prove the versioned contracts *bite*: the same validators the server can
run in debug mode reject malformed journal entries and API payloads, so a
stored/served shape from an old version is distinguishable from a current
one.
"""

import copy

import pytest

from crr.core import contracts


# --------------------------------------------------------------------------
# Canonical valid fixtures (the v1 shapes, exactly)
# --------------------------------------------------------------------------

def _journal_entry():
    return {
        "v": 1,
        "pid": 12345,
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


def _session_card():
    return {
        "pid": 12345,
        "state": "ghost",
        "cwd": "/home/u/project",
        "shell": "zsh",
        "host": "tmux",
        "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "sid_source": "guessed",
        "sid8": "8a1b2c3d",
        "last_prompt": "fix the reviver give-up guard",
        "model": "claude-opus-4-8",
        "duplicate_group": None,
        "tmux_session": None,
        "updated": "2026-07-23T00:00:00Z",
    }


def _archive_record():
    return {
        "v": 1,
        "reason": "superseded-on-register",
        "archived_at": "2026-07-24T00:00:00Z",
        "entry": _journal_entry(),
    }


def _sessions_payload():
    return {"contract": contracts.SESSIONS_CONTRACT_VERSION, "sessions": [_session_card()]}


def _diagnostics_payload():
    return {
        "contract": contracts.DIAGNOSTICS_CONTRACT_VERSION,
        "source": "journald",
        "summary": ["Out-of-memory: the host ran low on memory."],
        "boots": [{"index": -1, "boot_id": "…", "start": "…", "stop": "…"}],
        "prev_boot_errors": ["oom-killer: killed process 4242"],
        "host_events": ["reboot at 03:14"],
        "degraded": [],
    }


# --------------------------------------------------------------------------
# Version constants exist and are integers
# --------------------------------------------------------------------------

def test_version_constants_are_ints():
    assert isinstance(contracts.JOURNAL_SCHEMA_VERSION, int)
    assert isinstance(contracts.SESSIONS_CONTRACT_VERSION, int)
    assert isinstance(contracts.DIAGNOSTICS_CONTRACT_VERSION, int)


# --------------------------------------------------------------------------
# Journal schema v1
# --------------------------------------------------------------------------

def test_valid_journal_entry_passes():
    contracts.validate_journal_entry(_journal_entry())  # must not raise


def test_journal_wrong_version_rejected():
    e = _journal_entry()
    e["v"] = 2
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_missing_required_key_rejected():
    e = _journal_entry()
    del e["boot_id"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_unknown_key_rejected():
    e = _journal_entry()
    e["surprise"] = "extra"
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_bad_host_enum_rejected():
    e = _journal_entry()
    e["host"] = "carrier-pigeon"
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_bad_sid_source_rejected():
    e = _journal_entry()
    e["claude"]["sid_source"] = "vibes"
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_claude_missing_key_rejected():
    e = _journal_entry()
    del e["claude"]["session_id"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_claude_may_be_null():
    # A shell registers at start, before any claude launches: claude=null
    # is the honest "no rescuable session yet" state.
    e = _journal_entry()
    e["claude"] = None
    contracts.validate_journal_entry(e)  # must not raise


def test_journal_claude_missing_key_still_rejected_when_present():
    # Nullable does not mean "anything goes" — a partial claude object is
    # still invalid.
    e = _journal_entry()
    del e["claude"]["sid_source"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_pid_must_be_int():
    e = _journal_entry()
    e["pid"] = "12345"
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_revive_strikes_required_and_int():
    e = _journal_entry()
    del e["revive_strikes"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_revive_strikes_rejects_non_int():
    e = _journal_entry()
    e["revive_strikes"] = "3"
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


def test_journal_pid_true_bool_rejected():
    # bool is a subclass of int, so isinstance(True, int) is True. A pid of
    # True/False is nonsense and must be rejected, not silently accepted.
    e = _journal_entry()
    e["pid"] = True
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(e)


# --------------------------------------------------------------------------
# /api/sessions
# --------------------------------------------------------------------------

def test_valid_sessions_payload_passes():
    contracts.validate_sessions_payload(_sessions_payload())


def test_sessions_wrong_contract_version_rejected():
    p = _sessions_payload()
    p["contract"] = 999
    with pytest.raises(contracts.ContractError):
        contracts.validate_sessions_payload(p)


def test_sessions_bad_state_enum_rejected():
    p = _sessions_payload()
    p["sessions"][0]["state"] = "healthy"
    with pytest.raises(contracts.ContractError):
        contracts.validate_sessions_payload(p)


def test_sessions_card_missing_sid_source_rejected():
    # sid_source is the audit P3 provenance field; it must be contracted.
    p = _sessions_payload()
    del p["sessions"][0]["sid_source"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_sessions_payload(p)


def test_sessions_card_unknown_key_rejected():
    p = _sessions_payload()
    p["sessions"][0]["extra"] = 1
    with pytest.raises(contracts.ContractError):
        contracts.validate_sessions_payload(p)


def test_sessions_card_missing_model_rejected():
    # model (task #13) is a contracted card field, like last_prompt.
    p = _sessions_payload()
    del p["sessions"][0]["model"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_sessions_payload(p)


def test_sessions_card_model_must_be_str():
    p = _sessions_payload()
    p["sessions"][0]["model"] = 5
    with pytest.raises(contracts.ContractError):
        contracts.validate_sessions_payload(p)


def test_session_card_rejects_non_string_tmux_session():
    card = _session_card()
    card["tmux_session"] = 7
    with pytest.raises(contracts.ContractError):
        contracts.validate_session_card(card)


def test_sessions_payload_rejects_previous_contract_version():
    payload = _sessions_payload()
    payload["contract"] = 2
    with pytest.raises(contracts.ContractError):
        contracts.validate_sessions_payload(payload)


# --------------------------------------------------------------------------
# /api/diagnostics
# --------------------------------------------------------------------------

def test_valid_diagnostics_payload_passes():
    contracts.validate_diagnostics_payload(_diagnostics_payload())


def test_diagnostics_wrong_contract_version_rejected():
    p = _diagnostics_payload()
    p["contract"] = 999
    with pytest.raises(contracts.ContractError):
        contracts.validate_diagnostics_payload(p)


def test_diagnostics_missing_key_rejected():
    p = _diagnostics_payload()
    del p["degraded"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_diagnostics_payload(p)


def test_diagnostics_degraded_must_be_list():
    p = _diagnostics_payload()
    p["degraded"] = "journald"
    with pytest.raises(contracts.ContractError):
        contracts.validate_diagnostics_payload(p)


def test_diagnostics_summary_is_required_and_must_be_list():
    # v2 contract: the plain-English summary is a required list field.
    p = _diagnostics_payload()
    del p["summary"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_diagnostics_payload(p)
    p = _diagnostics_payload()
    p["summary"] = "out of memory"  # a bare string, not a list
    with pytest.raises(contracts.ContractError):
        contracts.validate_diagnostics_payload(p)


# --------------------------------------------------------------------------
# Archive record (audit P8 — State-first lineage)
# --------------------------------------------------------------------------

def test_valid_archive_record_passes():
    contracts.validate_archive_record(_archive_record())


def test_archive_wrong_version_rejected():
    r = _archive_record()
    r["v"] = 2
    with pytest.raises(contracts.ContractError):
        contracts.validate_archive_record(r)


def test_archive_bad_reason_rejected():
    r = _archive_record()
    r["reason"] = "because"
    with pytest.raises(contracts.ContractError):
        contracts.validate_archive_record(r)


def test_archive_superseded_on_launch_reason_accepted():
    r = _archive_record()
    r["reason"] = "superseded-on-launch"
    contracts.validate_archive_record(r)  # must not raise


def test_archive_dismissed_reason_accepted():
    r = _archive_record()
    r["reason"] = "dismissed"
    contracts.validate_archive_record(r)  # must not raise


def test_archive_detmuxed_reason_accepted():
    r = _archive_record()
    r["reason"] = "detmuxed"
    contracts.validate_archive_record(r)  # must not raise


def test_archive_missing_key_rejected():
    r = _archive_record()
    del r["archived_at"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_archive_record(r)


def test_archive_nested_entry_is_validated():
    # The preserved entry must itself be a valid journal entry.
    r = _archive_record()
    r["entry"]["host"] = "carrier-pigeon"
    with pytest.raises(contracts.ContractError):
        contracts.validate_archive_record(r)


def test_archive_requires_claude_bearing_entry():
    # Archiving exists to preserve revival data; a claude-less entry has
    # none, so it must not be archivable.
    r = _archive_record()
    r["entry"]["claude"] = None
    with pytest.raises(contracts.ContractError):
        contracts.validate_archive_record(r)


# --------------------------------------------------------------------------
# Validators must not mutate their input
# --------------------------------------------------------------------------

def test_validators_do_not_mutate_input():
    e = _journal_entry()
    before = copy.deepcopy(e)
    contracts.validate_journal_entry(e)
    assert e == before
