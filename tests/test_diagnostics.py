"""Diagnostics payload/parse tests (pure core, synthetic journald output)."""

import json

import pytest

from crr.core import contracts, diagnostics


def test_parse_boots_maps_fields_and_converts_time():
    raw = json.dumps([
        {"index": -1, "boot_id": "aaa", "first_entry": 1_000_000_000, "last_entry": 2_000_000_000},
        {"index": 0, "boot_id": "bbb", "first_entry": 3_000_000_000, "last_entry": 4_000_000_000},
    ])
    boots = diagnostics.parse_boots(raw, cap=10)
    assert [b["index"] for b in boots] == [-1, 0]
    assert boots[0]["boot_id"] == "aaa"
    assert boots[0]["start"].startswith("1970-01-01T00:16:40")  # 1e9 us = 1000 s


def test_parse_boots_caps_to_most_recent():
    raw = json.dumps([{"index": i, "boot_id": str(i), "first_entry": 0, "last_entry": 0}
                      for i in range(-5, 1)])
    boots = diagnostics.parse_boots(raw, cap=2)
    assert [b["index"] for b in boots] == [-1, 0]  # the two most recent


def test_parse_boots_tolerates_bad_json():
    assert diagnostics.parse_boots("not json", cap=10) == []


def test_parse_mac_boottime_yields_one_current_boot_record():
    boots = diagnostics.parse_mac_boottime("{ sec = 1000, usec = 0 } Wed Jul 23")
    assert len(boots) == 1
    assert boots[0]["boot_id"] == "1000"
    assert boots[0]["index"] == 0
    assert boots[0]["start"].startswith("1970-01-01T00:16:40")  # 1000 s past epoch
    assert boots[0]["stop"] == ""


def test_parse_mac_boottime_degrades_on_unparseable():
    assert diagnostics.parse_mac_boottime("no seconds here") == []


def test_filter_lines_keeps_matching_nonblank_lines_capped():
    text = "boot ok\nkernel panic: foo\n\nnormal wake event\nshutdown by user\n"
    hits = diagnostics.filter_lines(text, ("panic", "shutdown"), cap=10)
    assert hits == ["kernel panic: foo", "shutdown by user"]


def test_filter_lines_is_case_insensitive_and_caps():
    text = "Sleep now\nWAKE up\nDarkWake later\n"
    assert diagnostics.filter_lines(text, ("wake",), cap=1) == ["WAKE up"]


_PARAMS = {"lookback_boots": 1, "event_cap": 50, "line_cap": 200, "timeout_seconds": 5}


def test_build_payload_is_contract_valid():
    payload = diagnostics.build_payload(
        source="journald",
        boots=[{"index": 0, "boot_id": "x", "start": "", "stop": ""}],
        prev_boot_errors=["oom-killer: killed process 4242"],
        host_events=["reboot"],
        degraded=[],
        params=_PARAMS,
    )
    contracts.validate_diagnostics_payload(payload)
    assert payload["source"] == "journald"


def test_build_payload_records_params_verbatim():
    # F11: the generating caps/lookback/timeout travel with the payload
    # (audit P3/P5) instead of being lost the moment collect() returns.
    payload = diagnostics.build_payload(
        source="journald", boots=[], prev_boot_errors=[], host_events=[], degraded=[],
        params=_PARAMS,
    )
    assert payload["params"] == _PARAMS


def test_build_payload_auto_derives_a_plain_english_summary():
    # Callers that don't pass summary get one derived from the events, so the
    # dashboard + CLI always have the "why" without duplicating the call.
    payload = diagnostics.build_payload(
        source="journald", boots=[], prev_boot_errors=[],
        host_events=["Out of memory: Killed process 4242"], degraded=[],
        params=_PARAMS,
    )
    contracts.validate_diagnostics_payload(payload)
    assert payload["summary"]
    assert any("memory" in s.lower() for s in payload["summary"])


def test_build_payload_summary_is_clean_verdict_when_no_events():
    payload = diagnostics.build_payload(
        source="journald", boots=[], prev_boot_errors=[], host_events=[], degraded=[],
        params=_PARAMS,
    )
    assert len(payload["summary"]) == 1  # explicit "looks clean", never empty


def test_build_payload_records_degraded_sources():
    payload = diagnostics.build_payload(
        source="journald", boots=[], prev_boot_errors=[], host_events=[],
        degraded=["host_events"], params=_PARAMS,
    )
    contracts.validate_diagnostics_payload(payload)
    assert payload["degraded"] == ["host_events"]
