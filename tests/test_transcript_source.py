"""Transcript-source adapter tests — locate + reverse-read, synthetic files.

No real conversation content: the test writes its own JSONL transcript
(noise near the end, a real prompt just before it) under a fake HOME.
"""

import json
import os
from pathlib import Path

from crr.adapters import transcript_source
from crr.core.config import DEFAULTS


def _write_transcript(home: Path, sid: str, records, project="-home-u-proj"):
    d = home / ".claude" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _user(content, **extra):
    r = {"type": "user", "message": {"role": "user", "content": content}}
    r.update(extra)
    return r


def _user_tool_result():
    """A mid-turn tool-result record — the noise that fills the gap between a
    prompt and the reply before it on an agentic session."""
    return {"type": "user", "toolUseResult": {"ok": True},
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}


def _assistant(text, model=None):
    msg = {"role": "assistant", "content": text}
    if model is not None:
        msg["model"] = model
    return {"type": "assistant", "message": msg}


def _assistant_end(text, model="claude-opus-4-8"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": text, "model": model, "stop_reason": "end_turn"},
    }


def _assistant_synthetic():
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": "interrupted", "model": "<synthetic>"},
    }


def _tool_result_turn():
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
        "toolUseResult": {"stdout": "x"},
    }


def test_find_transcript_globs_any_project_dir(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [_user("hi")])
    assert transcript_source.find_transcript(sid, home=tmp_path) is not None
    assert transcript_source.find_transcript("no-such-sid", home=tmp_path) is None


def test_read_tail_facts_skips_trailing_noise(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    path = _write_transcript(tmp_path, sid, [
        _user("an older prompt"),
        _user("the real last prompt"),
        _assistant("answer", model="claude-opus-4-8"),
        _user([{"type": "tool_result", "content": "big output"}]),  # noise after it
        _user("<task-notification>bg task done</task-notification>"),
    ])
    facts = transcript_source.read_tail_facts(sid, cap=100, home=tmp_path)
    assert facts["last_prompt"] == "the real last prompt"
    assert facts["model"] == "claude-opus-4-8"
    assert facts["transcript_bytes"] == path.stat().st_size
    assert facts["transcript_bytes"] > 0


def test_read_tail_facts_reads_model_from_a_trailing_assistant_turn(tmp_path):
    # The model lives on assistant turns near the tail; skip <synthetic> ones.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("a prompt"),
        _assistant("real answer", model="claude-opus-5"),
        _assistant("interrupted", model="<synthetic>"),  # most recent, noise
    ])
    assert transcript_source.read_tail_facts(sid, cap=100, home=tmp_path)["model"] == "claude-opus-5"


def test_read_tail_facts_bounds_the_model_search_to_the_tail(tmp_path):
    # A real model always sits within a few lines of the tail (measured p99=37);
    # 1 in 3 transcripts have NO model at all. So the model search is bounded to
    # a tail window — otherwise a model-less transcript would be read in full on
    # every 5s poll. The prompt search stays unbounded (last prompt can be deep).
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    deep_model = _assistant("ancient answer", model="claude-opus-4-8")
    noise = [_user([{"type": "tool_result", "content": "x"}])
             for _ in range(transcript_source.MODEL_TAIL_LINES + 5)]
    _write_transcript(tmp_path, sid, [deep_model, _user("the real prompt"), *noise])
    facts = transcript_source.read_tail_facts(sid, cap=100, home=tmp_path)
    assert facts["last_prompt"] == "the real prompt"   # prompt still found (unbounded)
    assert facts["model"] == ""                          # model beyond the window -> unknown


def test_model_tail_lines_is_the_named_config_default():
    # Finding 6 (re-audit): the literal `200` used to be a second, undeduped
    # copy of `DEFAULTS["model_tail_lines"]` — pin them together so they
    # can't drift apart again.
    assert transcript_source.MODEL_TAIL_LINES == DEFAULTS["model_tail_lines"]


def test_read_tail_facts_missing_transcript_is_empty(tmp_path):
    assert transcript_source.read_tail_facts("nope", cap=100, home=tmp_path) == {
        "last_prompt": "", "model": "", "last_active": "",
        "last_reply": "", "title": "", "slug": "", "transcript_bytes": 0,
    }


def test_read_tail_facts_last_active_is_the_newest_turns_timestamp(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("older prompt", timestamp="2026-01-01T00:00:00Z"),
        _user("the real last prompt", timestamp="2026-01-02T00:00:00Z"),
    ])
    facts = transcript_source.read_tail_facts(sid, cap=100, home=tmp_path)
    assert facts["last_active"] == "2026-01-02T00:00:00Z"


def test_read_tail_facts_transcript_bytes_matches_file_size(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    path = _write_transcript(tmp_path, sid, [
        _user("a prompt", timestamp="2026-01-01T00:00:00Z"),
    ])
    facts = transcript_source.read_tail_facts(sid, cap=100, home=tmp_path)
    assert facts["transcript_bytes"] == path.stat().st_size


def test_read_tail_facts_honors_configured_model_tail_lines(tmp_path):
    # F18: the tail window is injectable (crr.core.config's model_tail_lines),
    # not a bare module constant — a caller can narrow it without touching code.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    near_model = _assistant("recent answer", model="claude-opus-4-8")
    noise = [_user([{"type": "tool_result", "content": "x"}]) for _ in range(10)]
    _write_transcript(tmp_path, sid, [near_model, _user("the real prompt"), *noise])
    facts = transcript_source.read_tail_facts(sid, cap=100, home=tmp_path, model_tail_lines=5)
    assert facts["last_prompt"] == "the real prompt"
    assert facts["model"] == ""  # narrowed window: 11 lines back is now out of range


def test_reverse_read_handles_lines_spanning_block_boundary(tmp_path):
    # A prompt padded past the block size must still be read whole.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    big = "x" * 200000
    _write_transcript(tmp_path, sid, [_user("real"), _user(big)])
    # The huge one is most recent; it isn't noise, so it comes back (capped).
    out = transcript_source.read_tail_facts(sid, cap=50, home=tmp_path)["last_prompt"]
    assert out == "x" * 50


def test_list_transcripts_returns_sids_and_mtimes_for_a_cwd(tmp_path):
    # The project dir encodes the cwd as path-with-slashes-as-dashes.
    _write_transcript(tmp_path, "sidA", [_user("a")], project="-home-u-proj")
    _write_transcript(tmp_path, "sidB", [_user("b")], project="-home-u-proj")
    # A different cwd's transcript must not leak in.
    _write_transcript(tmp_path, "other", [_user("c")], project="-home-u-elsewhere")

    got = transcript_source.list_transcripts("/home/u/proj", home=tmp_path)
    by_id = {t["session_id"]: t for t in got}
    assert set(by_id) == {"sidA", "sidB"}
    assert all(isinstance(t["mtime"], float) for t in got)


def test_list_transcripts_absent_project_dir_is_empty(tmp_path):
    # Unknown cwd (or Claude's encoding differs) → empty, so the caller
    # degrades to an untracked passthrough rather than erroring.
    assert transcript_source.list_transcripts("/no/such/cwd", home=tmp_path) == []


# --- list_all_transcripts / read_cwd (T-C — discovery) --------------------


def test_list_all_transcripts_enumerates_across_projects(tmp_path):
    sid_a = "aaaaaaaa-1111-4111-8111-111111111111"
    sid_b = "bbbbbbbb-2222-4222-8222-222222222222"
    _write_transcript(tmp_path, sid_a, [_user("a")], project="-home-u-projA")
    _write_transcript(tmp_path, sid_b, [_user("b")], project="-home-u-projB")

    got = transcript_source.list_all_transcripts(home=tmp_path)
    by_id = {t["session_id"]: t for t in got}
    assert set(by_id) == {sid_a, sid_b}
    assert by_id[sid_a]["cwd"] == "/home/u/projA"
    assert all(isinstance(t["mtime"], float) for t in got)


def test_list_all_transcripts_decodes_cwd_from_project_dir_name(tmp_path):
    sid = "aaaaaaaa-1111-4111-8111-111111111111"
    _write_transcript(tmp_path, sid, [_user("a")], project="-home-u-proj")
    got = transcript_source.list_all_transcripts(home=tmp_path)
    assert got[0]["cwd"] == "/home/u/proj"


def test_list_all_transcripts_skips_non_uuid_filenames(tmp_path):
    # A non-UUID stem can never round-trip through the /api/sid-action
    # UUID gate or ArchiveStore.path_for — filtered out here rather than
    # surfaced as a "discoverable" entry nothing can actually adopt.
    sid = "aaaaaaaa-1111-4111-8111-111111111111"
    _write_transcript(tmp_path, sid, [_user("a")], project="-home-u-proj")
    _write_transcript(tmp_path, "not-a-uuid", [_user("b")], project="-home-u-proj")
    got = transcript_source.list_all_transcripts(home=tmp_path)
    assert [t["session_id"] for t in got] == [sid]


def test_list_all_transcripts_absent_projects_dir_is_empty(tmp_path):
    assert transcript_source.list_all_transcripts(home=tmp_path) == []


def test_list_all_transcripts_no_content_read(tmp_path, monkeypatch):
    # Cheap by design: glob + stat only, no file open.
    sid = "aaaaaaaa-1111-4111-8111-111111111111"
    _write_transcript(tmp_path, sid, [_user("a")], project="-home-u-proj")
    real_open = open

    def guarded_open(*a, **kw):
        raise AssertionError("list_all_transcripts must not open file content")

    import builtins
    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        got = transcript_source.list_all_transcripts(home=tmp_path)
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
    assert len(got) == 1


def test_read_cwd_finds_the_stamped_cwd(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("a prompt", cwd="/home/u/Real-Path-With-Dashes"),
    ])
    assert transcript_source.read_cwd(sid, home=tmp_path) == "/home/u/Real-Path-With-Dashes"


def test_read_cwd_missing_transcript_is_none(tmp_path):
    assert transcript_source.read_cwd("no-such-sid", home=tmp_path) is None


def test_read_cwd_absent_field_is_none(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [_user("a prompt")])
    assert transcript_source.read_cwd(sid, home=tmp_path) is None


def test_read_cwd_bounded_to_scan_lines(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    noise = [_user("no cwd here") for _ in range(10)]
    _write_transcript(tmp_path, sid, [*noise, _user("has cwd", cwd="/home/u/proj")])
    # cwd sits past the tiny scan window -> not found (bounded, honest None).
    assert transcript_source.read_cwd(sid, home=tmp_path, scan_lines=5) is None
    # A large-enough window finds it.
    assert transcript_source.read_cwd(sid, home=tmp_path, scan_lines=20) == "/home/u/proj"


def test_reverse_lines_yields_end_to_start(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    assert [ln.strip() for ln in transcript_source._reversed_lines(p, block_size=2)] == ["c", "b", "a"]


# --- search_transcript / search_cwd (F1 — `crr recall`) -------------------


def test_search_transcript_finds_a_user_prompt(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("what is a fox"),
        _assistant("a small mammal"),
    ])
    matches = transcript_source.search_transcript(sid, "fox", cap=100, home=tmp_path)
    assert len(matches) == 1
    assert matches[0]["role"] == "user"
    assert matches[0]["text"] == "what is a fox"


def test_search_transcript_finds_assistant_text_too(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("tell me about mammals"),
        _assistant("a fox is a small mammal"),
    ])
    matches = transcript_source.search_transcript(sid, "fox", cap=100, home=tmp_path)
    assert len(matches) == 1
    assert matches[0]["role"] == "assistant"


def test_search_transcript_skips_tool_result_noise(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user([{"type": "tool_result", "content": "fox output"}]),
        _user("a real fox prompt"),
    ])
    matches = transcript_source.search_transcript(sid, "fox", cap=100, home=tmp_path)
    assert len(matches) == 1
    assert matches[0]["text"] == "a real fox prompt"


def test_search_transcript_no_match_is_empty_list(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [_user("hello there")])
    assert transcript_source.search_transcript(sid, "nonexistent", cap=100, home=tmp_path) == []


def test_search_transcript_missing_transcript_is_empty_list(tmp_path):
    assert transcript_source.search_transcript("no-such-sid", "fox", cap=100, home=tmp_path) == []


def test_search_transcript_preserves_chronological_order(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("first fox"),
        _assistant("no fox here"),
        _user("second fox"),
    ])
    matches = transcript_source.search_transcript(sid, "fox", cap=100, home=tmp_path)
    assert [m["text"] for m in matches] == ["first fox", "no fox here", "second fox"]
    assert [m["index"] for m in matches] == [0, 1, 2]


def test_search_cwd_tags_matches_with_their_session_id(tmp_path):
    _write_transcript(tmp_path, "sidA", [_user("a fox in project A")], project="-home-u-proj")
    _write_transcript(tmp_path, "sidB", [_user("a fox in project B")], project="-home-u-proj")
    # A different cwd's transcript must not leak in.
    _write_transcript(tmp_path, "sidC", [_user("a fox elsewhere")], project="-home-u-elsewhere")

    matches = transcript_source.search_cwd("/home/u/proj", "fox", cap=100, home=tmp_path)
    by_sid = {m["session_id"]: m for m in matches}
    assert set(by_sid) == {"sidA", "sidB"}
    assert by_sid["sidA"]["text"] == "a fox in project A"


def test_search_cwd_no_transcripts_is_empty_list(tmp_path):
    assert transcript_source.search_cwd("/no/such/cwd", "fox", cap=100, home=tmp_path) == []


# --- search_all (dashboard global recall) ---------------------------------

_SID_A = "aaaaaaaa-0000-4000-8000-000000000000"  # newest
_SID_B = "bbbbbbbb-0000-4000-8000-000000000000"
_SID_C = "cccccccc-0000-4000-8000-000000000000"  # oldest


def _write_dated(tmp_path, sid, records, project, mtime):
    p = _write_transcript(tmp_path, sid, records, project=project)
    os.utime(p, (mtime, mtime))
    return p


def test_search_all_scans_every_transcript_within_a_generous_budget(tmp_path):
    _write_dated(tmp_path, _SID_A, [_user("a fox in A")], "-home-u-a", 3000)
    _write_dated(tmp_path, _SID_B, [_user("a fox in B")], "-home-u-b", 2000)
    _write_dated(tmp_path, _SID_C, [_user("no match here")], "-home-u-c", 1000)
    res = transcript_source.search_all(
        "fox", snippet_cap=100, match_cap=10, byte_budget=10_000_000, home=tmp_path
    )
    assert {m["session_id"] for m in res["matches"]} == {_SID_A, _SID_B}
    assert res["scanned"] == 3
    assert res["skipped"] == 0


def test_search_all_stops_at_the_byte_budget_newest_first(tmp_path):
    pA = _write_dated(tmp_path, _SID_A, [_user("a fox in A"), _user("x" * 4000)], "-home-u-a", 3000)
    _write_dated(tmp_path, _SID_B, [_user("a fox in B")], "-home-u-b", 2000)
    _write_dated(tmp_path, _SID_C, [_user("a fox in C")], "-home-u-c", 1000)
    # budget only fits the newest (A); B and C are past it.
    res = transcript_source.search_all(
        "fox", snippet_cap=100, match_cap=10, byte_budget=pA.stat().st_size + 1, home=tmp_path
    )
    assert {m["session_id"] for m in res["matches"]} == {_SID_A}  # only the newest searched
    assert res["scanned"] == 1
    assert res["skipped"] == 2  # honest: two newest-first transcripts not searched


def test_search_all_truncates_to_match_cap(tmp_path):
    _write_dated(tmp_path, _SID_A, [_user("fox one"), _user("fox two"), _user("fox three")], "-home-u-a", 3000)
    res = transcript_source.search_all(
        "fox", snippet_cap=100, match_cap=2, byte_budget=10_000_000, home=tmp_path
    )
    assert len(res["matches"]) == 2


def test_search_all_no_transcripts_is_empty(tmp_path):
    res = transcript_source.search_all(
        "fox", snippet_cap=100, match_cap=5, byte_budget=10_000_000, home=tmp_path
    )
    assert res == {"matches": [], "scanned": 0, "skipped": 0}


# --- read_takeover_signal (`crr adopt --takeover` safety signal) ----------


def test_read_takeover_signal_assistant_end_tail(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    path = _write_transcript(tmp_path, sid, [
        _user("a prompt"),
        _assistant_end("the final answer"),
    ])
    sig = transcript_source.read_takeover_signal(sid, home=tmp_path)
    assert sig["tail_kind"] == "assistant-end"
    assert sig["mtime"] == path.stat().st_mtime
    assert sig["mtime"] > 0


def test_read_takeover_signal_skips_a_trailing_synthetic_record(tmp_path):
    # A <synthetic> assistant record (API error/interrupt) at the tail must
    # be transparent — the scan should skip it and report the prior REAL
    # assistant end_turn underneath it.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("a prompt"),
        _assistant_end("the real final answer"),
        _assistant_synthetic(),
    ])
    sig = transcript_source.read_takeover_signal(sid, home=tmp_path)
    assert sig["tail_kind"] == "assistant-end"


def test_read_takeover_signal_mid_turn_tail(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("a prompt"),
        _assistant_end("an answer"),
        _tool_result_turn(),
    ])
    sig = transcript_source.read_takeover_signal(sid, home=tmp_path)
    assert sig["tail_kind"] == "mid-turn"


def test_read_takeover_signal_absent_transcript_is_honest_empty(tmp_path):
    assert transcript_source.read_takeover_signal("no-such-sid", home=tmp_path) == {
        "mtime": 0.0, "tail_kind": "",
    }


def test_read_takeover_signal_stats_mtime_after_reading_the_tail(tmp_path, monkeypatch):
    # Ordering is load-bearing (see the docstring): an append landing between
    # the tail read and the mtime stat must pair the already-read tail with
    # the FRESH mtime -> seconds_idle small -> caller keeps waiting (the safe
    # direction). Stat-first would instead pair a stale-quiet mtime with a
    # tail that had already changed. Simulate the concurrent append inside
    # the backward-read generator itself, so it lands strictly between the
    # two reads no matter how the implementation is refactored. An
    # all-"other" transcript (no turn-bearing record at all) forces the scan
    # to walk every line rather than early-exiting on the first record, so
    # the simulated append is guaranteed to land before the stat runs; it
    # also exercises the "present but no turn-bearing record found" branch.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    path = _write_transcript(tmp_path, sid, [{"type": "permission-mode", "mode": "plan"}])
    bumped = path.stat().st_mtime + 100
    real_reversed_lines = transcript_source._reversed_lines

    def spy(p, *a, **kw):
        yield from real_reversed_lines(p, *a, **kw)
        os.utime(p, (bumped, bumped))  # the "concurrent append" lands here

    monkeypatch.setattr(transcript_source, "_reversed_lines", spy)
    sig = transcript_source.read_takeover_signal(sid, home=tmp_path)
    assert sig["tail_kind"] == ""
    assert sig["mtime"] == bumped  # stat happened AFTER the tail read completed


def test_search_transcript_survives_invalid_utf8_bytes(tmp_path):
    # A UnicodeDecodeError (ValueError subclass) raised mid-line-iteration
    # happens BEFORE json.loads's try/except and would escape the outer
    # `except OSError` in search_transcript, killing the whole search (and,
    # under --all, the whole sweep) with a traceback. errors="replace" on
    # the open() call keeps a corrupt file from raising at all.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    d = tmp_path / ".claude" / "projects" / "-home-u-proj"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    good_line = json.dumps({
        "type": "user", "message": {"role": "user", "content": "a fox prompt"},
    }).encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(good_line + b"\n")
        fh.write(b"\xff\xfe not valid utf-8 \x80\x81\n")  # invalid UTF-8 bytes
        fh.write(good_line + b"\n")
    matches = transcript_source.search_transcript(sid, "fox", cap=100, home=tmp_path)
    assert len(matches) == 2
    assert all(m["text"] == "a fox prompt" for m in matches)


def test_read_tail_facts_returns_the_reply_before_the_last_prompt(tmp_path):
    _write_transcript(tmp_path, _SID_A, [
        _assistant("an older answer"),
        _user("first question"),
        _assistant("the answer that preceded the last prompt"),
        _user("the most recent prompt"),
    ])
    facts = transcript_source.read_tail_facts(_SID_A, 200, home=tmp_path)
    assert facts["last_prompt"] == "the most recent prompt"
    assert facts["last_reply"] == "the answer that preceded the last prompt"


def test_read_tail_facts_reply_is_empty_beyond_the_window(tmp_path):
    # A reply further back than reply_tail_lines is honestly "" rather than
    # forcing a whole-file read on the 5s poll path.
    records = [_assistant("a very old answer")]
    records += [_user_tool_result() for _ in range(30)]
    records += [_user("the most recent prompt")]
    _write_transcript(tmp_path, _SID_B, records)
    facts = transcript_source.read_tail_facts(_SID_B, 200, home=tmp_path, reply_tail_lines=5)
    assert facts["last_prompt"] == "the most recent prompt"
    assert facts["last_reply"] == ""


def test_search_all_skips_excluded_dirs(tmp_path):
    # Recall sweeps the SAME transcript pool as discovery, so it must honor
    # the same exclusion list — otherwise the byte budget is spent on
    # tool-internal transcripts and one can surface as a match.
    _write_dated(tmp_path, _SID_A, [_user("a fox in the project")], "-home-u-proj", 3000)
    _write_dated(tmp_path, _SID_B, [_user("a fox in the observer log")],
                 "-home-u--claude-mem-observer-sessions", 4000)  # newest
    res = transcript_source.search_all(
        "fox", snippet_cap=100, match_cap=10, byte_budget=10_000_000,
        home=tmp_path, exclude_dirs=[".claude-mem"],
    )
    assert {m["session_id"] for m in res["matches"]} == {_SID_A}
    assert res["scanned"] == 1  # the excluded one was never read


def test_search_all_without_exclusions_scans_everything(tmp_path):
    _write_dated(tmp_path, _SID_A, [_user("a fox here")], "-home-u-proj", 3000)
    _write_dated(tmp_path, _SID_B, [_user("a fox there")], "-home-u--claude-mem-x", 4000)
    res = transcript_source.search_all(
        "fox", snippet_cap=100, match_cap=10, byte_budget=10_000_000, home=tmp_path,
    )
    assert res["scanned"] == 2


# --- raw-bytes prefilter (recall coverage without the parse cost) ---------

def test_prefilter_skips_files_that_cannot_match(tmp_path, monkeypatch):
    # A transcript whose bytes don't contain the term can't possibly match,
    # so it must never be parsed. Proven by making the parser explode.
    _write_transcript(tmp_path, _SID_A, [_user("nothing relevant here")])
    def boom(path):
        raise AssertionError("parsed a file that cannot match: %s" % path)
    monkeypatch.setattr(transcript_source, "_read_records", boom)
    assert transcript_source.search_transcript(_SID_A, "dokploy", cap=100, home=tmp_path) == []


def test_prefilter_still_finds_a_real_match(tmp_path):
    _write_transcript(tmp_path, _SID_A, [_user("let's try dokploy today")])
    out = transcript_source.search_transcript(_SID_A, "dokploy", cap=100, home=tmp_path)
    assert len(out) == 1 and "dokploy" in out[0]["text"]


def test_prefilter_is_case_insensitive_like_the_matcher(tmp_path):
    _write_transcript(tmp_path, _SID_A, [_user("Let's try DOKPLOY today")])
    assert len(transcript_source.search_transcript(_SID_A, "dokploy", cap=100, home=tmp_path)) == 1


def test_prefilter_finds_a_term_spanning_a_read_chunk_boundary(tmp_path):
    # FALSE-NEGATIVE TRAP: a chunked scan must overlap, or a term split
    # across two reads is silently lost.
    filler = "x" * 70000
    _write_transcript(tmp_path, _SID_A, [_user(filler + " dokploy " + filler)])
    out = transcript_source.search_transcript(_SID_A, "dokploy", cap=200000, home=tmp_path)
    assert len(out) == 1


def test_query_with_json_escaped_chars_is_not_prefiltered(tmp_path):
    # FALSE-NEGATIVE TRAP: a quote is stored ESCAPED in JSON (\"), so a raw
    # byte test for it would wrongly skip the file. Such queries must bypass
    # the prefilter and be parsed normally.
    _write_transcript(tmp_path, _SID_A, [_user('he said "hello" loudly')])
    out = transcript_source.search_transcript(_SID_A, 'said "hello"', cap=200, home=tmp_path)
    assert len(out) == 1


def test_non_ascii_query_is_not_prefiltered(tmp_path):
    # Byte-level lowercasing only folds ASCII, so a non-ASCII query must not
    # rely on the prefilter (it would risk a false negative).
    _write_transcript(tmp_path, _SID_A, [_user("réunion notes")])
    out = transcript_source.search_transcript(_SID_A, "RÉUNION", cap=200, home=tmp_path)
    assert len(out) == 1


def test_search_all_zero_budget_means_unlimited(tmp_path):
    _write_dated(tmp_path, _SID_A, [_user("a fox")], "-home-u-a", 3000)
    _write_dated(tmp_path, _SID_B, [_user("a fox")], "-home-u-b", 2000)
    _write_dated(tmp_path, _SID_C, [_user("a fox")], "-home-u-c", 1000)
    res = transcript_source.search_all(
        "fox", snippet_cap=100, match_cap=10, byte_budget=0, home=tmp_path)
    assert res["scanned"] == 3 and res["skipped"] == 0
    assert len(res["matches"]) == 3
