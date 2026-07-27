"""Transcript-source adapter tests — locate + reverse-read, synthetic files.

No real conversation content: the test writes its own JSONL transcript
(noise near the end, a real prompt just before it) under a fake HOME.
"""

import json
from pathlib import Path

from crr.adapters import transcript_source


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
    _write_transcript(tmp_path, sid, [
        _user("an older prompt"),
        _user("the real last prompt"),
        _assistant("answer", model="claude-opus-4-8"),
        _user([{"type": "tool_result", "content": "big output"}]),  # noise after it
        _user("<task-notification>bg task done</task-notification>"),
    ])
    facts = transcript_source.read_tail_facts(sid, cap=100, home=tmp_path)
    assert facts == {"last_prompt": "the real last prompt", "model": "claude-opus-4-8"}


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


def test_read_tail_facts_missing_transcript_is_empty(tmp_path):
    assert transcript_source.read_tail_facts("nope", cap=100, home=tmp_path) == {
        "last_prompt": "", "model": ""
    }


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


def test_reverse_lines_yields_end_to_start(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    assert [ln.strip() for ln in transcript_source._reversed_lines(p, block_size=2)] == ["c", "b", "a"]
