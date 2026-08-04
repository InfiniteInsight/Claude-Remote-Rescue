"""Discovery core tests (T-C) — pure, no filesystem, synthetic transcripts.

Covers `untracked` (excludes journaled sids, sorts by recency, empty input,
sid8 derivation, mtime fallback) and `build_adopted_entry`/`adopted_pid`
(the "recoverable entry from an external record" builder).
"""

from crr.core import contracts, discovery

_SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
_SID_B = "bbbbbbbb-2222-4222-8222-222222222222"
_SID_C = "cccccccc-3333-4333-8333-333333333333"


def _t(sid, **extra):
    row = {
        "session_id": sid,
        "cwd": "/home/u/proj",
        "last_active": "",
        "transcript_bytes": 0,
        "last_prompt": "",
    }
    row.update(extra)
    return row


# --- untracked --------------------------------------------------------


def test_untracked_excludes_journaled_sids():
    transcripts = [_t(_SID_A), _t(_SID_B)]
    out = discovery.untracked({_SID_A}, transcripts)
    assert [r["session_id"] for r in out] == [_SID_B]


def test_untracked_empty_input_is_empty():
    assert discovery.untracked(set(), []) == []


def test_untracked_all_journaled_is_empty():
    transcripts = [_t(_SID_A), _t(_SID_B)]
    assert discovery.untracked({_SID_A, _SID_B}, transcripts) == []


def test_untracked_sid8_is_derived_from_session_id():
    out = discovery.untracked(set(), [_t(_SID_A)])
    assert out[0]["sid8"] == _SID_A[:8]


def test_untracked_row_shape():
    out = discovery.untracked(set(), [_t(_SID_A, cwd="/x", last_active="2026-01-01T00:00:00Z",
                                          transcript_bytes=42, last_prompt="hi")])
    assert out == [{
        "session_id": _SID_A,
        "sid8": _SID_A[:8],
        "cwd": "/x",
        "last_active": "2026-01-01T00:00:00Z",
        "transcript_bytes": 42,
        "last_prompt": "hi",
    }]


def test_untracked_sorts_most_recent_first_by_last_active():
    transcripts = [
        _t(_SID_A, last_active="2026-01-01T00:00:00Z"),
        _t(_SID_B, last_active="2026-01-03T00:00:00Z"),
        _t(_SID_C, last_active="2026-01-02T00:00:00Z"),
    ]
    out = discovery.untracked(set(), transcripts)
    assert [r["session_id"] for r in out] == [_SID_B, _SID_C, _SID_A]


def test_untracked_sort_is_epoch_not_raw_string():
    # Two differently-but-both-validly-serialized ISO timestamps: a raw
    # string comparison would order these wrong ('Z' < '+' lexically, but
    # here the '+00:00' one is actually earlier in real time... construct a
    # case where naive string comparison and epoch comparison disagree).
    transcripts = [
        _t(_SID_A, last_active="2026-01-01T23:00:00+02:00"),  # 21:00 UTC
        _t(_SID_B, last_active="2026-01-01T22:00:00Z"),        # 22:00 UTC (later)
    ]
    out = discovery.untracked(set(), transcripts)
    assert [r["session_id"] for r in out] == [_SID_B, _SID_A]


def test_untracked_falls_back_to_mtime_when_last_active_is_empty():
    transcripts = [
        _t(_SID_A, last_active="", mtime=2_000_000_000.0),  # ~2033: later
        _t(_SID_B, last_active="2026-01-01T00:00:00Z", mtime=1.0),
    ]
    out = discovery.untracked(set(), transcripts)
    # A carries only a raw mtime later than B's ISO timestamp -> A wins.
    assert [r["session_id"] for r in out] == [_SID_A, _SID_B]


def test_untracked_no_recency_info_sorts_last():
    transcripts = [
        _t(_SID_A, last_active="", mtime=None),
        _t(_SID_B, last_active="2026-01-01T00:00:00Z"),
    ]
    out = discovery.untracked(set(), transcripts)
    assert [r["session_id"] for r in out] == [_SID_B, _SID_A]


def test_untracked_naive_last_active_is_treated_as_utc_not_local():
    # fromisoformat happily parses a timestamp with no UTC offset; .timestamp()
    # on a naive datetime silently reads it as LOCAL time, which would
    # misorder it against an aware sibling by the host's UTC offset. This
    # must not raise, and must not silently drop the row.
    transcripts = [
        _t(_SID_A, last_active="2026-01-01T00:00:00"),  # naive
        _t(_SID_B, last_active="2025-01-01T00:00:00Z"),  # aware, clearly older
    ]
    out = discovery.untracked(set(), transcripts)
    assert [r["session_id"] for r in out] == [_SID_A, _SID_B]


def test_untracked_unparseable_last_active_falls_back_to_mtime():
    transcripts = [_t(_SID_A, last_active="not-a-timestamp", mtime=5.0)]
    out = discovery.untracked(set(), transcripts)
    # Doesn't raise; falls back to mtime rather than crashing the sort.
    assert [r["session_id"] for r in out] == [_SID_A]


def test_untracked_missing_optional_fields_default_honestly():
    out = discovery.untracked(set(), [{"session_id": _SID_A}])
    assert out == [{
        "session_id": _SID_A,
        "sid8": _SID_A[:8],
        "cwd": "",
        "last_active": "",
        "transcript_bytes": 0,
        "last_prompt": "",
    }]


# --- build_adopted_entry / adopted_pid ---------------------------------


def test_build_adopted_entry_is_a_valid_journal_entry():
    entry = discovery.build_adopted_entry(_SID_A, "/home/u/proj", "2026-08-03T00:00:00+00:00")
    contracts.validate_journal_entry(entry)  # must not raise


def test_build_adopted_entry_fields():
    entry = discovery.build_adopted_entry(_SID_A, "/home/u/proj", "2026-08-03T00:00:00+00:00")
    assert entry["cwd"] == "/home/u/proj"
    assert entry["tmux_session"] is None
    assert entry["boot_id"] == discovery.ADOPTED_BOOT_ID
    assert entry["updated"] == "2026-08-03T00:00:00+00:00"
    assert entry["claude"] == {
        "session_id": _SID_A,
        "sid_source": "guessed",
        # NOT "now" — see discovery._UNKNOWN_STARTED: epoch zero so
        # resume.verify_guessed upgrades this to "verified" on the very
        # next status/poll pass instead of paying a permanent glob.
        "started": discovery._UNKNOWN_STARTED,
    }


def test_adopted_entry_started_lets_verify_guessed_upgrade_immediately():
    # The whole point of _UNKNOWN_STARTED: any real transcript mtime must
    # count as "activity since start" so an adopted entry doesn't stay
    # sid_source="guessed" (and keep costing a poll-path glob) forever.
    from crr.core.resume import verify_guessed

    entry = discovery.build_adopted_entry(_SID_A, "/home/u/proj", "2026-08-03T00:00:00+00:00")
    transcripts = [{"session_id": _SID_A, "mtime": 1.0}]  # any real mtime > epoch 0
    updated = verify_guessed(entry, transcripts, "2026-08-03T00:00:01+00:00")
    assert updated is not None
    assert updated["claude"]["sid_source"] == "verified"


def test_build_adopted_entry_pid_matches_adopted_pid():
    entry = discovery.build_adopted_entry(_SID_A, "/x", "2026-08-03T00:00:00+00:00")
    assert entry["pid"] == discovery.adopted_pid(_SID_A)


def test_adopted_pid_is_deterministic():
    assert discovery.adopted_pid(_SID_A) == discovery.adopted_pid(_SID_A)


def test_adopted_pid_differs_across_sids_in_practice():
    assert discovery.adopted_pid(_SID_A) != discovery.adopted_pid(_SID_B)


def test_adopted_pid_is_well_above_any_real_linux_pid():
    # pid_max defaults to 4194304 (2**22); the synthetic range starts well
    # above even a raised ceiling.
    assert discovery.adopted_pid(_SID_A) >= 100_000_000


# --- filter_and_page (dashboard discoverable modal: search + pagination) ---

def _row(sid, cwd, prompt=""):
    return {"session_id": sid, "sid8": sid[:8], "cwd": cwd, "last_active": "",
            "transcript_bytes": 0, "last_prompt": prompt}


def _rows(n):
    return [_row(f"{i:08d}-0000-4000-8000-000000000000", f"/home/u/p{i}") for i in range(n)]


def test_filter_and_page_slices_a_page_and_reports_the_total():
    out = discovery.filter_and_page(_rows(50), query="", offset=0, limit=20)
    assert len(out["rows"]) == 20
    assert out["total"] == 50      # how many exist in all
    assert out["filtered"] == 50   # how many matched the query
    assert out["offset"] == 0 and out["limit"] == 20


def test_filter_and_page_respects_offset():
    out = discovery.filter_and_page(_rows(50), query="", offset=40, limit=20)
    assert len(out["rows"]) == 10  # last partial page
    assert out["rows"][0]["cwd"] == "/home/u/p40"


def test_filter_and_page_filters_on_cwd_and_sid_case_insensitively():
    rows = [_row("aaaaaaaa-0000-4000-8000-000000000000", "/home/u/Storefront"),
            _row("bbbbbbbb-0000-4000-8000-000000000000", "/home/u/payments")]
    by_cwd = discovery.filter_and_page(rows, query="STOREFRONT", offset=0, limit=10)
    assert [r["cwd"] for r in by_cwd["rows"]] == ["/home/u/Storefront"]
    assert by_cwd["total"] == 2 and by_cwd["filtered"] == 1  # total is unfiltered
    by_sid = discovery.filter_and_page(rows, query="bbbbbbbb", offset=0, limit=10)
    assert len(by_sid["rows"]) == 1


def test_filter_and_page_offset_past_the_end_is_empty_not_an_error():
    out = discovery.filter_and_page(_rows(5), query="", offset=99, limit=20)
    assert out["rows"] == [] and out["filtered"] == 5
