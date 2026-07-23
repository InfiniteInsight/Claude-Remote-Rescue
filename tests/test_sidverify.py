import os
import time

from crr import journal, sidverify


def _write_transcript(root, cwd, name, mtime=None):
    slug = sidverify.project_slug(cwd)
    proj_dir = root / slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / ("%s.jsonl" % name)
    path.write_text("{}\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_project_slug_replaces_slashes():
    assert sidverify.project_slug("/home/u/my-project") == "-home-u-my-project"


def test_newest_transcript_picks_latest_mtime(tmp_path, monkeypatch):
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/u/proj"
    _write_transcript(tmp_path, cwd, "older", mtime=1000)
    newer = _write_transcript(tmp_path, cwd, "newer", mtime=2000)

    got = sidverify.newest_transcript(cwd)
    assert got == newer


def test_newest_transcript_none_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert sidverify.newest_transcript("/nope") is None


def test_guess_sid_returns_stem(tmp_path, monkeypatch):
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/u/proj"
    _write_transcript(tmp_path, cwd, "abc-123", mtime=1000)
    assert sidverify.guess_sid(cwd) == "abc-123"


def test_guess_sid_empty_when_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert sidverify.guess_sid("/home/u/proj") == ""


def test_newest_transcript_after_requires_strictly_newer(tmp_path, monkeypatch):
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/u/proj"
    _write_transcript(tmp_path, cwd, "same-time", mtime=5000)
    # Not strictly newer than the threshold -> not picked up.
    assert sidverify.newest_transcript_after(cwd, after_epoch=5000) is None
    assert sidverify.newest_transcript_after(cwd, after_epoch=4999) is not None


def test_verify_sid_marks_verified_when_newer_transcript_exists(
    tmp_path, monkeypatch, crr_state
):
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/u/proj"
    entry = journal.new_entry(
        pid=555,
        cwd=cwd,
        shell="bash",
        host="tab",
        boot_id="boot-x",
        claude={"session_id": "guessed-sid", "verified": False},
    )
    journal.write_entry(entry)

    started = time.time()
    _write_transcript(tmp_path, cwd, "authoritative-sid", mtime=started + 5)

    ok = sidverify.verify_sid(555, started, wait_seconds=0)
    assert ok is True

    updated = journal.read_entry(555)
    assert updated["claude"]["session_id"] == "authoritative-sid"
    assert updated["claude"]["verified"] is True


def test_verify_sid_keeps_guess_when_nothing_newer(tmp_path, monkeypatch, crr_state):
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/u/proj"
    entry = journal.new_entry(
        pid=556,
        cwd=cwd,
        shell="bash",
        host="tab",
        boot_id="boot-x",
        claude={"session_id": "guessed-sid", "verified": False},
    )
    journal.write_entry(entry)

    started = time.time()
    # Transcript older than the launch: not picked up.
    _write_transcript(tmp_path, cwd, "stale-sid", mtime=started - 100)

    ok = sidverify.verify_sid(556, started, wait_seconds=0)
    assert ok is False

    unchanged = journal.read_entry(556)
    assert unchanged["claude"]["session_id"] == "guessed-sid"
    assert unchanged["claude"]["verified"] is False


def test_verify_sid_missing_entry_returns_false(crr_state):
    assert sidverify.verify_sid(999999, time.time(), wait_seconds=0) is False


def test_verify_sid_same_second_stale_transcript_not_verified(
    tmp_path, monkeypatch, crr_state
):
    """Regression: a whole-second-truncated 'started' timestamp must not
    make an already-existing (pre-launch) transcript look newer just
    because it landed in the same wall-clock second. verify_sid is given
    the precise (sub-second) started time crr's own `now` command would
    produce; the pre-existing transcript's mtime, even if it floors to
    the same integer second, must not be treated as "after" it."""
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/u/proj"
    entry = journal.new_entry(
        pid=557,
        cwd=cwd,
        shell="bash",
        host="tab",
        boot_id="boot-x",
        claude={"session_id": "guessed-sid", "verified": False},
    )
    journal.write_entry(entry)

    now = time.time()
    started = now + 0.3  # launch happens slightly after the pre-existing file
    # Pre-existing transcript (the one guess-sid already found) lands
    # earlier in wall-clock time but in the same integer second.
    _write_transcript(tmp_path, cwd, "guessed-sid", mtime=now)

    ok = sidverify.verify_sid(557, started, wait_seconds=0)
    assert ok is False
    unchanged = journal.read_entry(557)
    assert unchanged["claude"]["verified"] is False
