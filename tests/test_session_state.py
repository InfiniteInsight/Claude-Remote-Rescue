"""Claude Code's own per-process state files (spec 2026-08-09, Phase 1).

`~/.claude/sessions/<pid>.json` is written by Claude Code itself and carries
`bridgeSessionId` (null when the phone link is down), `status`, and
`waitingFor`. Undocumented internal state: every read degrades to a missing
entry or an honest `field_present=False`, never to a fabricated value.
"""

import json

import pytest

from crr.adapters import session_state

SID_A = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SID_B = "1234abcd-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _write(home, pid, sid, **fields):
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "sessionId": sid}
    payload.update(fields)
    (d / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_reads_a_connected_session(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId="session_013C", status="idle")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].bridge_session_id == "session_013C"
    assert got[SID_A].field_present is True
    assert got[SID_A].status == "idle"
    assert got[SID_A].pid == 100


def test_a_null_bridge_is_read_as_none_with_the_field_present(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId=None, status="idle")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].bridge_session_id is None
    assert got[SID_A].field_present is True   # null is an ANSWER, not an absence


def test_a_file_without_the_field_reports_field_present_false(tmp_path):
    _write(tmp_path, 100, SID_A, status="idle")
    assert session_state.read_all(tmp_path)[SID_A].field_present is False


def test_a_garbage_typed_bridge_field_is_not_a_readable_answer(tmp_path):
    # Neither a string nor null: a future Claude Code reshaping the field
    # (say to `{"id": ...}`) must not read as "the link is down". Folding it
    # into a bare None with field_present=True would make core classify it
    # `unreachable`, and the watchdog would kick a live process on the
    # strength of a value it could not parse. Unreadable degrades to unknown.
    _write(tmp_path, 100, SID_A, bridgeSessionId={"id": "session_013C"}, status="idle")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].field_present is False
    assert got[SID_A].bridge_session_id is None


def test_waiting_for_is_carried(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId=None,
           status="waiting", waitingFor="permission prompt")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].status == "waiting"
    assert got[SID_A].waiting_for == "permission prompt"


def test_absent_waiting_for_is_an_empty_string_not_none(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId=None, status="idle")
    assert session_state.read_all(tmp_path)[SID_A].waiting_for == ""


@pytest.mark.parametrize("stale_pid,live_pid", [(100, 200), (200, 100)])
def test_the_newest_file_wins_when_a_session_has_several(tmp_path, stale_pid, live_pid):
    # Observed live: one session id had NINETEEN state files from successive
    # claude processes. Only the newest describes the running one.
    #
    # Parametrised in BOTH directions on purpose. `Path.glob` yields files in
    # the filesystem's own order, which is neither sorted nor creation-
    # ordered, so a single direction lets an implementation that keeps the
    # first- or last-globbed file pass by luck. One of these two cases puts
    # the stale file last and the other puts it first, so mtime is the only
    # rule that satisfies both.
    import os, time
    _write(tmp_path, stale_pid, SID_A, bridgeSessionId=None, status="idle")
    _write(tmp_path, live_pid, SID_A, bridgeSessionId="session_new", status="busy")
    stale = tmp_path / ".claude" / "sessions" / f"{stale_pid}.json"
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    got = session_state.read_all(tmp_path)
    assert got[SID_A].pid == live_pid
    assert got[SID_A].bridge_session_id == "session_new"


def test_separate_sessions_are_kept_separate(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId="session_a", status="idle")
    _write(tmp_path, 200, SID_B, bridgeSessionId=None, status="idle")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].bridge_session_id == "session_a"
    assert got[SID_B].bridge_session_id is None


def test_a_corrupt_file_is_skipped_without_raising(tmp_path):
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path, 100, SID_A, bridgeSessionId="session_013C", status="idle")
    got = session_state.read_all(tmp_path)
    assert SID_A in got            # the good file still read
    assert len(got) == 1


def test_a_file_without_a_session_id_is_skipped(tmp_path):
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    (d / "9.json").write_text(json.dumps({"pid": 9}), encoding="utf-8")
    assert session_state.read_all(tmp_path) == {}


def test_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert session_state.read_all(tmp_path) == {}
