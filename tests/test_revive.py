import subprocess

from crr import classify, journal, revive
from crr.result import EXIT_GAVE_UP, EXIT_NO_TMUX, EXIT_REFUSED


def write_entry(pid, *, sid=None, verified=None, revived=0, boot_id="old-boot"):
    claude = None
    if sid is not None:
        claude = {"session_id": sid, "started": "2026-01-01T00:00:00+00:00"}
        if verified is not None:
            claude["verified"] = verified
    entry = journal.new_entry(
        pid=pid, cwd="/proj", shell="zsh", host="tab", boot_id=boot_id, claude=claude
    )
    entry["revived"] = revived
    journal.write_entry(entry)
    return entry


class SpawnRecorder:
    def __init__(self, monkeypatch, returncode=0):
        self.calls = []
        self.returncode = returncode
        monkeypatch.setattr(revive, "_spawn_tmux", self._spawn)
        monkeypatch.setattr(revive, "tmux_available", lambda: True)

    def _spawn(self, argv):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, "", "")


def test_revive_uses_word_form_argv(monkeypatch):
    rec = SpawnRecorder(monkeypatch)
    entry = write_entry(100, sid="abc-123", verified=True)
    res = revive.revive_entry(entry)
    assert res.ok is True
    assert res.status == "revived"
    assert len(rec.calls) == 1
    argv = rec.calls[0]
    # [lesson: word-form exec] a list of words, never a joined shell string
    assert isinstance(argv, list)
    assert all(isinstance(word, str) for word in argv)
    assert argv == [
        "tmux", "new-session", "-d", "-s", "crr-100", "-c", "/proj",
        "claude", "--resume", "abc-123",
    ]
    # Journal updated: counter bumped, tmux session recorded.
    got = journal.read_entry(100)
    assert got["revived"] == 1
    assert got["tmux_session"] == "crr-100"


def test_revive_unverified_sid_falls_back_to_picker(monkeypatch):
    rec = SpawnRecorder(monkeypatch)
    entry = write_entry(101, sid="guessed-sid", verified=False)
    res = revive.revive_entry(entry)
    assert res.ok is True
    argv = rec.calls[0]
    # Bare --resume (picker); the possibly-wrong sid must not appear.
    assert argv[-2:] == ["claude", "--resume"]
    assert "guessed-sid" not in argv


def test_revive_no_sid_refused(monkeypatch):
    rec = SpawnRecorder(monkeypatch)
    entry = write_entry(102)
    res = revive.revive_entry(entry)
    assert res.ok is False
    assert res.status == "no-sid"
    assert res.exit_code == EXIT_REFUSED
    assert rec.calls == []


def test_give_up_guard_archives_instead_of_rerevive(monkeypatch):
    rec = SpawnRecorder(monkeypatch)
    entry = write_entry(103, sid="abc", revived=1)
    res = revive.revive_entry(entry)
    assert res.ok is False
    assert res.status == "gave-up"
    assert res.exit_code == EXIT_GAVE_UP
    assert rec.calls == []  # never re-revived
    assert journal.read_entry(103) is None
    archived = journal.list_archived()
    assert len(archived) == 1
    assert "give-up" in archived[0]["archive_reason"]


def test_revive_without_tmux_is_distinct_failure(monkeypatch):
    monkeypatch.setattr(revive, "tmux_available", lambda: False)
    entry = write_entry(104, sid="abc")
    res = revive.revive_entry(entry)
    assert res.ok is False
    assert res.status == "no-tmux"
    assert res.exit_code == EXIT_NO_TMUX


def test_tmux_failure_propagates(monkeypatch):
    rec = SpawnRecorder(monkeypatch, returncode=1)
    entry = write_entry(105, sid="abc")
    res = revive.revive_entry(entry)
    assert res.ok is False
    assert res.status == "tmux-failed"
    # Failed spawn must not consume a revival attempt.
    assert journal.read_entry(105)["revived"] == 0


def test_revive_all_only_touches_crashed_with_sid(monkeypatch):
    rec = SpawnRecorder(monkeypatch)
    write_entry(200, sid="sid-200")  # crashed, has sid -> revived
    write_entry(201)  # crashed, no sid -> skipped
    write_entry(202, sid="sid-202")  # will classify live -> skipped

    def fake_classify(entry, boot=None):
        return classify.LIVE if entry["pid"] == 202 else classify.CRASHED

    monkeypatch.setattr(classify, "classify", fake_classify)
    results = revive.revive_all()
    assert [r.pid for r in results] == [200]
    assert results[0].ok is True
    assert len(rec.calls) == 1
