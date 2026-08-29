"""Tab-spawn health store and doctor formatting (spec 2026-08-29)."""

import json

from crr.core import tab_health


def test_read_is_none_when_absent(tmp_path):
    assert tab_health.TabHealthStore(tmp_path).read() is None


def test_record_then_read_round_trip(tmp_path):
    store = tab_health.TabHealthStore(tmp_path)
    store.record(tab_health.TIER_AUMID, "alias stub failed",
                 now="2026-08-29T00:00:00Z", boot_id="b1")
    got = store.read()
    assert got["tier"] == tab_health.TIER_AUMID
    assert got["detail"] == "alias stub failed"
    assert got["ts"] == "2026-08-29T00:00:00Z"
    assert got["boot_id"] == "b1"


def test_corrupt_file_reads_as_none(tmp_path):
    (tmp_path / tab_health.FILENAME).write_text("not json", encoding="utf-8")
    assert tab_health.TabHealthStore(tmp_path).read() is None


def test_non_dict_reads_as_none(tmp_path):
    (tmp_path / tab_health.FILENAME).write_text("[1, 2]", encoding="utf-8")
    assert tab_health.TabHealthStore(tmp_path).read() is None


def test_future_version_reads_as_none(tmp_path):
    (tmp_path / tab_health.FILENAME).write_text(
        json.dumps({"v": 99, "tier": "wt"}), encoding="utf-8")
    assert tab_health.TabHealthStore(tmp_path).read() is None


def test_record_overwrites_the_previous_record(tmp_path):
    store = tab_health.TabHealthStore(tmp_path)
    store.record(tab_health.TIER_WT, "", now="2026-08-29T00:00:00Z", boot_id="b1")
    store.record(tab_health.TIER_CONSOLE, "wt gone",
                 now="2026-08-29T01:00:00Z", boot_id="b1")
    assert store.read()["tier"] == tab_health.TIER_CONSOLE


def test_doctor_line_no_record_is_neutral():
    label, ok, detail = tab_health.doctor_line(None)
    assert ok is True
    assert "not yet exercised" in detail


def test_doctor_line_wt_tier_is_plain_ok():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_WT, "detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is True
    assert "wt.exe" in detail
    # The alias note belongs only to the aumid tier.
    assert "App execution aliases" not in detail


def test_doctor_line_aumid_tier_carries_the_alias_note():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_AUMID, "detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is True
    assert "app package" in detail
    assert "App execution aliases" in detail
    # Never claim the alias IS broken: wt_probe cannot distinguish a disabled
    # alias from a context where wt.exe cannot exec at all.
    assert "alias is broken" not in detail
    assert "alias is disabled" not in detail


def test_doctor_line_console_tier_says_separate_window():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_CONSOLE, "detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is True
    assert "separate window" in detail


def test_doctor_line_none_tier_warns_with_the_error():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_NONE, "detail": "boom",
         "ts": "2026-08-29T00:00:00Z"})
    assert ok is False
    assert "boom" in detail


def test_doctor_line_always_shows_the_timestamp():
    for tier in (tab_health.TIER_WT, tab_health.TIER_AUMID,
                 tab_health.TIER_CONSOLE, tab_health.TIER_NONE):
        _, _, detail = tab_health.doctor_line(
            {"tier": tier, "detail": "", "ts": "2026-08-29T12:34:56Z"})
        assert "2026-08-29T12:34:56Z" in detail, tier


def test_doctor_line_unknown_tier_does_not_crash():
    label, ok, detail = tab_health.doctor_line(
        {"tier": "martian", "detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is None
