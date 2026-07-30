"""Resume-sid derivation + verification tests (pure core, audit P3).

Covers the confidence taxonomy for resume/continue/picker launches:
- explicit ``--resume <sid>`` is certain (verified if its transcript exists);
- ``--continue`` / picker has no sid → the newest transcript, marked guessed;
- a guessed sid upgrades to verified only when its own transcript shows
  activity AFTER the session started (silence never confirms).
"""

from datetime import datetime

from crr.core import resume


def _t(sid, mtime):
    return {"session_id": sid, "mtime": mtime}


# All crr-written sids are UUIDs (injected uuid4 / transcript filename
# stems), so fixtures use UUID literals throughout.
_AAA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_BBB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_ZZZ = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_OLD = "11111111-1111-4111-8111-111111111111"
_NEWEST = "22222222-2222-4222-8222-222222222222"
_MID = "33333333-3333-4333-8333-333333333333"


# --- derive_resume_sid ----------------------------------------------------

def test_explicit_sid_with_a_transcript_is_verified():
    ts = [_t(_AAA, 10.0), _t(_BBB, 20.0)]
    assert resume.derive_resume_sid(_BBB, ts) == (_BBB, "verified")


def test_explicit_sid_without_a_transcript_is_only_guessed():
    # The user named it, but nothing confirms it yet — an honest guess-strength
    # claim, not verified.
    assert resume.derive_resume_sid(_ZZZ, [_t(_AAA, 10.0)]) == (_ZZZ, "guessed")


def test_no_explicit_sid_picks_the_newest_transcript_as_guessed():
    ts = [_t(_OLD, 10.0), _t(_NEWEST, 30.0), _t(_MID, 20.0)]
    assert resume.derive_resume_sid(None, ts) == (_NEWEST, "guessed")


def test_no_explicit_sid_and_no_transcripts_yields_none():
    # Nothing to journal — the shim passes claude through untracked, as today.
    assert resume.derive_resume_sid(None, []) is None
    assert resume.derive_resume_sid("", []) is None


def test_derive_rejects_non_uuid_explicit_sid_without_guessing():
    """A junk --resume arg must yield None (untracked), NEVER a confident
    guess of a different session."""
    transcripts = [{"session_id": "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55", "mtime": 5.0}]
    assert resume.derive_resume_sid("../tabs/99", transcripts) is None


def test_derive_ignores_non_uuid_transcript_stems():
    transcripts = [{"session_id": "not-a-uuid", "mtime": 9.0},
                   {"session_id": "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55", "mtime": 5.0}]
    sid, source = resume.derive_resume_sid(None, transcripts)
    assert sid == "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55" and source == "guessed"


# --- verify_guessed -------------------------------------------------------

def _entry(sid, source, started):
    return {
        "pid": 1, "cwd": "/p", "claude": {
            "session_id": sid, "sid_source": source, "started": started,
        },
    }


_STARTED = "2026-07-25T12:00:00+00:00"
_STARTED_EPOCH = datetime.fromisoformat(_STARTED).timestamp()


def test_guessed_upgrades_to_verified_when_its_transcript_is_active_since_start():
    entry = _entry("sid1", "guessed", _STARTED)
    ts = [_t("sid1", _STARTED_EPOCH + 60)]  # written a minute after launch
    updated = resume.verify_guessed(entry, ts, now="2026-07-25T12:05:00+00:00")
    assert updated is not None
    assert updated["claude"]["sid_source"] == "verified"
    assert updated["claude"]["session_id"] == "sid1"  # sid unchanged


def test_guessed_stays_guessed_when_no_activity_since_start():
    # An idle resumed session may never touch the file — silence is not proof.
    entry = _entry("sid1", "guessed", _STARTED)
    ts = [_t("sid1", _STARTED_EPOCH - 5)]  # last touched BEFORE this launch
    assert resume.verify_guessed(entry, ts, now="2026-07-25T12:05:00+00:00") is None


def test_guessed_stays_guessed_when_its_transcript_is_absent():
    entry = _entry("sid1", "guessed", _STARTED)
    assert resume.verify_guessed(entry, [_t("other", _STARTED_EPOCH + 99)], now=_STARTED) is None


def test_non_guessed_entries_are_left_alone():
    for source in ("injected", "verified"):
        entry = _entry("sid1", source, _STARTED)
        ts = [_t("sid1", _STARTED_EPOCH + 60)]
        assert resume.verify_guessed(entry, ts, now=_STARTED) is None


def test_claude_less_entry_is_ignored():
    assert resume.verify_guessed({"claude": None}, [_t("x", 9e9)], now=_STARTED) is None


def test_unparseable_started_never_false_verifies():
    entry = _entry("sid1", "guessed", "not-a-timestamp")
    assert resume.verify_guessed(entry, [_t("sid1", 9e9)], now=_STARTED) is None
