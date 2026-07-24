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


def test_find_transcript_globs_any_project_dir(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [_user("hi")])
    assert transcript_source.find_transcript(sid, home=tmp_path) is not None
    assert transcript_source.find_transcript("no-such-sid", home=tmp_path) is None


def test_read_last_prompt_skips_trailing_noise(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_transcript(tmp_path, sid, [
        _user("an older prompt"),
        _user("the real last prompt"),
        _user([{"type": "tool_result", "content": "big output"}]),  # noise after it
        _user("<task-notification>bg task done</task-notification>"),
    ])
    assert transcript_source.read_last_prompt(sid, cap=100, home=tmp_path) == "the real last prompt"


def test_read_last_prompt_missing_transcript_is_empty(tmp_path):
    assert transcript_source.read_last_prompt("nope", cap=100, home=tmp_path) == ""


def test_reverse_read_handles_lines_spanning_block_boundary(tmp_path):
    # A prompt padded past the block size must still be read whole.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    big = "x" * 200000
    _write_transcript(tmp_path, sid, [_user("real"), _user(big)])
    # The huge one is most recent; it isn't noise, so it comes back (capped).
    out = transcript_source.read_last_prompt(sid, cap=50, home=tmp_path)
    assert out == "x" * 50


def test_reverse_lines_yields_end_to_start(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    assert [ln.strip() for ln in transcript_source._reversed_lines(p, block_size=2)] == ["c", "b", "a"]
