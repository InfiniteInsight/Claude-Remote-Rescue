"""Transcript-source adapter tests — locate + reverse-read, synthetic files.

No real conversation content: the test writes its own JSONL transcript
(noise near the end, a real prompt just before it) under a fake HOME.
"""

import json
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


def _assistant(text, model=None):
    msg = {"role": "assistant", "content": text}
    if model is not None:
        msg["model"] = model
    return {"type": "assistant", "message": msg}


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
        "last_prompt": "", "model": "", "last_active": "", "transcript_bytes": 0,
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
