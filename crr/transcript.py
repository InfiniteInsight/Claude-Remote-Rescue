"""Last-prompt extractor: the most recent genuine human prompt of a session.

Claude Code writes one JSONL transcript per session at

    ~/.claude/projects/<slug>/<sid>.jsonl

where <slug> is the session cwd with every non-alphanumeric character
mapped to ``-`` (verified against a real installation: ``/home/user`` ->
``-home-user``).

Transcripts grow large; the extractor streams the file BACKWARDS in
fixed-size blocks and stops at the first genuine prompt -- the whole file
is never loaded (performance requirement: transcript reads must stay
comfortably inside the dashboard's 5s poll at 25+ sessions).

Skip-list (each category was discovered as real garbage on real session
cards in ccresume):

- tool results (user-role messages whose content is a tool_result block)
- ``<command-name>`` / local-command wrapper messages and their caveats
- ``<task-notification>`` messages from background tasks
- ``<system-reminder>`` content (stripped; message skipped if empty after)
- compaction / continuation summaries
- meta / sidechain messages and every non-user role
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_BLOCK_SIZE = 64 * 1024
DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # give up after scanning this much tail
DEFAULT_LIMIT = 120

_SID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_WS_RE = re.compile(r"\s+")

# A message whose (stripped) text starts with any of these is not a
# genuine human prompt.
_SKIP_PREFIXES = (
    "<command-name>",
    "<local-command",
    "<task-notification>",
    "Caveat: the messages below",
    "This session is being continued from a previous conversation",
)


def cwd_slug(cwd: str) -> str:
    """Map a cwd to its transcript-directory slug (non-alnum -> ``-``)."""
    return _NON_ALNUM_RE.sub("-", cwd)


def projects_dir() -> Path:
    override = os.environ.get("CRR_CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects"


def transcript_path(sid: str, cwd: str, projects: Optional[Path] = None) -> Optional[Path]:
    """Path of the transcript for *sid* in *cwd*, or None for an invalid sid.

    The sid is validated against the uuid regex before being used in a
    filename: a tampered journal entry must not become a path traversal.
    """
    if not sid or not _SID_RE.match(sid):
        return None
    base = projects if projects is not None else projects_dir()
    return base / cwd_slug(cwd) / (sid + ".jsonl")


# ---------------------------------------------------------------------------
# Backwards block streaming


def _iter_lines_backwards(
    path: Path,
    block_size: int = DEFAULT_BLOCK_SIZE,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Iterator[bytes]:
    """Yield the lines of *path* last-first, reading fixed-size blocks from
    the end. Lines spanning block boundaries are reassembled; at most
    *max_bytes* of the file tail is ever read."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        stop = max(0, pos - max_bytes)
        buf = b""
        while pos > stop:
            read_size = min(block_size, pos - stop)
            pos -= read_size
            fh.seek(pos)
            buf = fh.read(read_size) + buf
            lines = buf.split(b"\n")
            # lines[0] may be a partial line continued in the next
            # (earlier) block; hold it back.
            buf = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if pos == 0 and buf.strip():
            yield buf


# ---------------------------------------------------------------------------
# Message filtering


def _text_of(content) -> Optional[str]:
    """Plain text of a user message's content, or None when the content is
    not prompt-like (e.g. contains tool_result blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                return None  # tool results are never human prompts
            if btype == "text":
                parts.append(block.get("text") or "")
        return "\n".join(parts) if parts else None
    return None


def _genuine_prompt(obj: dict) -> Optional[str]:
    """The genuine human prompt text of one transcript record, or None."""
    if obj.get("type") != "user":
        return None
    if obj.get("isMeta") or obj.get("isSidechain") or obj.get("isCompactSummary"):
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    text = _text_of(message.get("content"))
    if text is None:
        return None
    text = _SYSTEM_REMINDER_RE.sub("", text).strip()
    if not text:
        return None
    for prefix in _SKIP_PREFIXES:
        if text.startswith(prefix):
            return None
    return text


def last_prompt(
    sid: str,
    cwd: str,
    projects: Optional[Path] = None,
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    limit: int = DEFAULT_LIMIT,
) -> Optional[str]:
    """The most recent genuine human prompt of session *sid* in *cwd*,
    whitespace-collapsed and truncated to ~*limit* chars; None when the
    transcript is missing or contains no genuine prompt in its tail."""
    path = transcript_path(sid, cwd, projects)
    if path is None or not path.is_file():
        return None
    try:
        for raw in _iter_lines_backwards(path, block_size=block_size, max_bytes=max_bytes):
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            text = _genuine_prompt(obj)
            if text is None:
                continue
            text = _WS_RE.sub(" ", text).strip()
            if len(text) > limit:
                text = text[: limit - 1].rstrip() + "…"
            return text
    except OSError:
        return None
    return None
