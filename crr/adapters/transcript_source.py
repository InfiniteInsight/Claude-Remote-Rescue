"""Transcript-source adapter — locate + reverse-read Claude Code transcripts.

Claude Code stores one JSONL transcript per session at
``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``. We find it by
globbing on the session id (the filename), which avoids depending on the
exact cwd-encoding scheme.

Reads stream BACKWARD from the end with early exit: the last human prompt
is near the tail, so this stays cheap even on a multi-megabyte transcript
(the poll path must hold well under the page's cadence at many sessions).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from crr.core import transcript


def find_transcript(session_id: str, home: Path | None = None) -> Path | None:
    home = home or Path.home()
    projects = home / ".claude" / "projects"
    if not projects.is_dir():
        return None
    for match in projects.glob(f"*/{session_id}.jsonl"):
        return match
    return None


def _project_dir_name(cwd: str) -> str:
    """Claude Code encodes a cwd as its path with '/' replaced by '-'."""
    return cwd.replace("/", "-")


def list_transcripts(cwd: str, home: Path | None = None) -> list[dict]:
    """Return ``[{"session_id", "mtime"}, ...]`` for the transcripts of ``cwd``.

    Empty when the project dir is absent (unknown cwd, or Claude's encoding
    differs), so the resume path degrades to an untracked passthrough rather
    than raising. Used to guess/verify a resume sid, never on the poll path.
    """
    home = home or Path.home()
    project = (home / ".claude" / "projects" / _project_dir_name(cwd))
    if not project.is_dir():
        return []
    out = []
    for path in project.glob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append({"session_id": path.stem, "mtime": mtime})
    return out


def _reversed_lines(path: Path, block_size: int = 65536) -> Iterator[str]:
    """Yield non-empty lines of ``path`` from the end to the start."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        pos = fh.tell()
        carry = b""
        while pos > 0:
            read = min(block_size, pos)
            pos -= read
            fh.seek(pos)
            chunk = fh.read(read) + carry
            parts = chunk.split(b"\n")
            carry = parts[0]  # leftmost part may span into the earlier block
            for line in reversed(parts[1:]):
                if line.strip():
                    yield line.decode("utf-8", "replace")
        if carry.strip():
            yield carry.decode("utf-8", "replace")


# A real model id always sits within a few lines of the tail (measured on
# 3243 live transcripts: p50=3, p99=37 lines back), but ~1 in 3 transcripts
# carry NO model at all (older Claude Code, or all-<synthetic> tails). So the
# model search is bounded to this tail window: past it we stop looking for a
# model, or a model-less transcript would be read in full on every poll. The
# prompt search stays unbounded — the last human prompt can be genuinely deep.
MODEL_TAIL_LINES = 200


def read_tail_facts(session_id: str, cap: int, home: Path | None = None) -> dict[str, str]:
    """Most recent real prompt + model for ``session_id`` in ONE backward read.

    Both facts live near the tail, so a single reverse read fills both and
    early-exits — collapsing what would be two independent reads (each paying
    ``find_transcript``'s cross-project glob and the first 64KB block) into
    one on the poll path. The model search is bounded to the tail window (see
    ``MODEL_TAIL_LINES``); the prompt search is not. Missing/absent transcript
    degrades to honest empty strings, never a fabricated value.
    """
    facts = {"last_prompt": "", "model": ""}
    path = find_transcript(session_id, home)
    if path is None:
        return facts
    try:
        for i, line in enumerate(_reversed_lines(path)):
            in_model_window = i < MODEL_TAIL_LINES
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                record = None
            if record is not None and not facts["last_prompt"]:
                prompt = transcript.extract_prompt(record)
                if prompt is not None:
                    facts["last_prompt"] = transcript.clean_display(prompt, cap)
            if record is not None and not facts["model"] and in_model_window:
                model = transcript.extract_model(record)
                if model is not None:
                    facts["model"] = model
            # Stop once the prompt is found and the model is either found or can
            # no longer appear (past the tail window).
            if facts["last_prompt"] and (facts["model"] or not in_model_window):
                break
    except OSError:
        return facts
    return facts
