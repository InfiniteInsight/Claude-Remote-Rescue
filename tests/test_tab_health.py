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


# --- Finding 1: empty detail must not render a dangling colon/double space -

def test_doctor_line_none_tier_with_empty_detail_renders_cleanly():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_NONE, "detail": "",
         "ts": "2026-08-29T00:00:00Z"})
    assert ok is False
    assert "  " not in detail
    assert ": " not in detail
    assert " —" in detail  # the timestamp clause is still attached


# --- Finding 2: current_boot_id marks a record from before the last reboot -

def test_doctor_line_matching_boot_id_has_no_staleness_marker():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_WT, "detail": "", "ts": "2026-08-29T00:00:00Z",
         "boot_id": "b1"},
        current_boot_id="b1")
    assert "reboot" not in detail


def test_doctor_line_differing_boot_id_adds_staleness_marker():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_WT, "detail": "", "ts": "2026-08-29T00:00:00Z",
         "boot_id": "b1"},
        current_boot_id="b2")
    assert "reboot" in detail


def test_doctor_line_no_current_boot_id_has_no_staleness_marker():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_WT, "detail": "", "ts": "2026-08-29T00:00:00Z",
         "boot_id": "b1"})
    assert "reboot" not in detail


def test_doctor_line_staleness_marker_appears_for_every_tier():
    for tier in (tab_health.TIER_WT, tab_health.TIER_AUMID,
                 tab_health.TIER_CONSOLE, tab_health.TIER_NONE):
        _, _, detail = tab_health.doctor_line(
            {"tier": tier, "detail": "x", "ts": "2026-08-29T00:00:00Z",
             "boot_id": "old"},
            current_boot_id="new")
        assert "reboot" in detail, tier


def test_doctor_line_staleness_marker_survives_unrecognized_tier():
    _, ok, detail = tab_health.doctor_line(
        {"tier": "martian", "detail": "", "ts": "2026-08-29T00:00:00Z",
         "boot_id": "old"},
        current_boot_id="new")
    assert ok is None
    assert "reboot" in detail


# --- Finding 3: genuinely independent edge cases (not in the plan) --------

def test_doctor_line_record_missing_tier_key_does_not_crash():
    label, ok, detail = tab_health.doctor_line(
        {"detail": "", "ts": "2026-08-29T00:00:00Z"})
    assert ok is None
    assert "unrecognized" in detail


def test_doctor_line_record_missing_ts_key_does_not_crash():
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_WT, "detail": ""})
    assert ok is True
    assert "unknown time" in detail


def test_doctor_line_detail_with_braces_and_newlines_is_not_mangled():
    weird = "boom {oops} % s \n line2"
    label, ok, detail = tab_health.doctor_line(
        {"tier": tab_health.TIER_NONE, "detail": weird,
         "ts": "2026-08-29T00:00:00Z"})
    assert weird in detail


def test_read_when_parent_directory_does_not_exist_returns_none(tmp_path):
    missing_parent = tmp_path / "does" / "not" / "exist"
    assert tab_health.TabHealthStore(missing_parent).read() is None


def test_record_then_read_round_trips_every_tier_constant(tmp_path):
    for tier in (tab_health.TIER_WT, tab_health.TIER_AUMID,
                 tab_health.TIER_CONSOLE, tab_health.TIER_NONE):
        store = tab_health.TabHealthStore(tmp_path)
        store.record(tier, "some detail", now="2026-08-29T00:00:00Z",
                     boot_id="b1")
        got = store.read()
        assert got["tier"] == tier
        assert got["detail"] == "some detail"


# --- record_from_spawner (spec 2026-08-29, Task 3) --------------------------

class _Spawner:
    def __init__(self, last_tier, last_confirmed):
        self.last_tier = last_tier
        self.last_confirmed = last_confirmed


def test_record_from_spawner_is_a_no_op_with_no_store():
    # Must not raise — every ops.py/reviver.py call site passes tab_health
    # unconditionally, and most callers have never wired a store.
    tab_health.record_from_spawner(None, _Spawner(tab_health.TIER_WT, True),
                                    now="2026-08-29T00:00:00Z", boot_id="b1")


def test_record_from_spawner_skips_a_spawner_with_no_tier(tmp_path):
    # macOS/Linux spawners have no last_tier at all — safe to call
    # unconditionally after any spawn, on any platform.
    store = tab_health.TabHealthStore(tmp_path)
    record_from_spawner = tab_health.record_from_spawner
    record_from_spawner(store, object(), now="2026-08-29T00:00:00Z", boot_id="b1")
    assert store.read() is None


def test_record_from_spawner_records_confirmed_tier_1_with_no_detail(tmp_path):
    store = tab_health.TabHealthStore(tmp_path)
    tab_health.record_from_spawner(
        store, _Spawner(tab_health.TIER_WT, True),
        now="2026-08-29T00:00:00Z", boot_id="b1")
    got = store.read()
    assert got["tier"] == tab_health.TIER_WT
    assert got["detail"] == ""


def test_record_from_spawner_records_unconfirmed_tier_2_as_launched(tmp_path):
    # Tier 2/3 fire through Start-Process — a zero exit proves the launch,
    # not the tab, so "launched, unconfirmed" is an honest, non-contradictory
    # description of what actually happened.
    store = tab_health.TabHealthStore(tmp_path)
    tab_health.record_from_spawner(
        store, _Spawner(tab_health.TIER_AUMID, False),
        now="2026-08-29T00:00:00Z", boot_id="b1")
    got = store.read()
    assert got["tier"] == tab_health.TIER_AUMID
    assert got["detail"] == "launched, unconfirmed"


class _RaisingStore:
    """A store whose ``record`` fails like a read-only or full state dir."""

    def record(self, tier, detail="", *, now, boot_id):
        raise OSError("no space left on device")


def test_record_from_spawner_swallows_a_store_write_oserror():
    # Finding 1: telemetry must never outrank the operation it describes.
    # write_json_atomic raises OSError on a read-only/full state dir; that
    # must not propagate out of record_from_spawner, which every ops.py and
    # reviver.py call site invokes unconditionally after a spawn attempt —
    # including inside reviver's "never raises" _try_open_tab.
    tab_health.record_from_spawner(
        _RaisingStore(), _Spawner(tab_health.TIER_WT, True),
        now="2026-08-29T00:00:00Z", boot_id="b1")  # must not raise


def test_record_from_spawner_tier_none_uses_the_given_error(tmp_path):
    # Finding 7: doctor's detail-bearing TIER_NONE branch was dead — no
    # production call site ever passed a detail. Threading the caught
    # exception through as `error` makes "[warn] naming the last error"
    # (the spec's own words) actually reachable.
    store = tab_health.TabHealthStore(tmp_path)
    tab_health.record_from_spawner(
        store, _Spawner(tab_health.TIER_NONE, False),
        now="2026-08-29T00:00:00Z", boot_id="b1", error=OSError("ENOEXEC"))
    assert store.read()["detail"] == "ENOEXEC"


def test_record_from_spawner_tier_none_with_no_error_keeps_empty_detail(tmp_path):
    store = tab_health.TabHealthStore(tmp_path)
    tab_health.record_from_spawner(
        store, _Spawner(tab_health.TIER_NONE, False),
        now="2026-08-29T00:00:00Z", boot_id="b1")
    assert store.read()["detail"] == ""


def test_record_from_spawner_non_none_tier_ignores_a_given_error(tmp_path):
    # `error` only matters for TIER_NONE — a confirmed/unconfirmed tier's
    # detail must stay exactly what it was, never overwritten by a
    # bystander exception (defensive: no call site does this today, but the
    # parameter's contract should not silently depend on nobody trying).
    store = tab_health.TabHealthStore(tmp_path)
    tab_health.record_from_spawner(
        store, _Spawner(tab_health.TIER_AUMID, False),
        now="2026-08-29T00:00:00Z", boot_id="b1", error=OSError("irrelevant"))
    assert store.read()["detail"] == "launched, unconfirmed"


def test_record_from_spawner_tier_none_never_says_launched(tmp_path):
    # Regression: TIER_NONE means EVERY tier failed — nothing launched at
    # all. Recording "launched, unconfirmed" here would render doctor's
    # self-contradicting "no launcher worked: launched, unconfirmed".
    store = tab_health.TabHealthStore(tmp_path)
    tab_health.record_from_spawner(
        store, _Spawner(tab_health.TIER_NONE, False),
        now="2026-08-29T00:00:00Z", boot_id="b1")
    got = store.read()
    assert got["tier"] == tab_health.TIER_NONE
    assert got["detail"] == ""
    _, ok, message = tab_health.doctor_line(got)
    assert ok is False
    assert "launched" not in message
    assert message == "no launcher worked — last attempt 2026-08-29T00:00:00Z"
