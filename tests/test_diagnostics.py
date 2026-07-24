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


def test_build_payload_is_contract_valid():
    payload = diagnostics.build_payload(
        source="journald",
        boots=[{"index": 0, "boot_id": "x", "start": "", "stop": ""}],
        prev_boot_errors=["oom-killer: killed process 4242"],
        host_events=["reboot"],
        degraded=[],
    )
    contracts.validate_diagnostics_payload(payload)
    assert payload["source"] == "journald"


def test_build_payload_records_degraded_sources():
    payload = diagnostics.build_payload(
        source="journald", boots=[], prev_boot_errors=[], host_events=[],
        degraded=["host_events"],
    )
    contracts.validate_diagnostics_payload(payload)
    assert payload["degraded"] == ["host_events"]
