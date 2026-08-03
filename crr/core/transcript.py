"""Last-prompt extractor — pull the last real human prompt from a
Claude Code transcript, dropping the noise that clutters real cards.

Pure core: operates on already-parsed JSONL records, so it is testable
with synthetic fixtures (no real conversation content) and the adapter
owns finding + reverse-reading the transcript file.

The skip-list is empirical (each entry was a class of garbage seen on real
cards): tool-result turns, meta lines, slash-command wrappers, local
command output, task-notifications, injected system-reminders, caveat
banners, interrupt markers, and compaction continuations. New classes join
the list here.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# Injected blocks stripped from an otherwise-real prompt (they get appended
# to a genuine user turn, so strip rather than skip the whole message).
_STRIP_RE = re.compile(
    r"<(system-reminder|user-prompt-submit-hook)>.*?</\1>",
    re.DOTALL,
)

# If the cleaned text starts with one of these, the whole message is noise.
_SKIP_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<task-notification>",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
    "Caveat:",
    "This session is being continued",
    "[Request interrupted",
)


def _candidate_text(record: Mapping[str, Any]) -> str | None:
    """The human-typed text of a user turn, or None if it isn't one."""
    if not isinstance(record, Mapping):
        return None
    if record.get("type") != "user" or record.get("isMeta"):
        return None
    message = record.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, Mapping) and b.get("type") == "text"
        ]
        # A list with no text blocks is a tool-result/image turn — not a prompt.
        return "\n".join(texts) if texts else None
    return None


def extract_prompt(record: Mapping[str, Any]) -> str | None:
    """Return the real prompt text of ``record``, or None if it is noise."""
    text = _candidate_text(record)
    if text is None:
        return None
    text = _STRIP_RE.sub("", text).strip()
    if not text:
        return None
    if any(text.startswith(p) for p in _SKIP_PREFIXES):
        return None
    return text


def extract_model(record: Mapping[str, Any]) -> str | None:
    """Return the model id of an assistant turn, or None if it isn't one.

    Skips ``<synthetic>`` turns — the assistant records Claude Code writes for
    API errors and interrupts carry no real model. Reading backward, those
    cluster at the tail, so a naive reader would stamp ``<synthetic>`` on the
    card; skipping them here keeps walking to the last genuine model.
    """
    if not isinstance(record, Mapping) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, Mapping):
        return None
    model = message.get("model")
    if not isinstance(model, str) or not model or model == "<synthetic>":
        return None
    return model


def clean_display(text: str, cap: int) -> str:
    """Collapse whitespace to one line and cap the length for a card."""
    return " ".join(text.split())[:cap]


def last_prompt(records: Iterable[Mapping[str, Any]], *, cap: int) -> str:
    """Most recent real prompt across ``records`` (chronological), capped."""
    for record in reversed(list(records)):
        prompt = extract_prompt(record)
        if prompt is not None:
            return clean_display(prompt, cap)
    return ""


def extract_timestamp(record: Mapping[str, Any]) -> str | None:
    """Return the ISO ``timestamp`` of ``record``, or None if it has none."""
    if not isinstance(record, Mapping):
        return None
    ts = record.get("timestamp")
    return ts if isinstance(ts, str) and ts else None


def extract_cwd(record: Mapping[str, Any]) -> str | None:
    """Return the record's stamped ``cwd``, or None if absent/malformed.

    Claude Code stamps ``cwd`` on every non-header record (the session-start
    and snapshot lines near the top of a transcript don't carry it; the
    first real turn does). This is the AUTHORITATIVE source for a session's
    working directory — used by discovery (T-C) to seed an adopted entry's
    ``cwd`` instead of decoding the project directory name, which is lossy
    (a literal ``-`` inside a real path component, e.g. this repo's own
    ``Claude-Remote-Rescue``, is indistinguishable from an encoded ``/``)
    and can hand a revive path a directory that doesn't exist.
    """
    if not isinstance(record, Mapping):
        return None
    cwd = record.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _assistant_text(record: Mapping[str, Any]) -> str | None:
    """The text of a real assistant turn, or None (tool-use/no-text/synthetic
    turns). Mirrors ``extract_model``'s ``<synthetic>`` skip: the assistant
    records Claude Code writes for API errors and interrupts are noise, not
    real conversation — they must not surface in a recall search either."""
    if not isinstance(record, Mapping) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return None
    if message.get("model") == "<synthetic>":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, Mapping) and b.get("type") == "text"
        ]
        # A list with no text blocks (tool_use only) is not a display turn.
        return "\n".join(texts) if texts else None
    return None


def search(
    records: Iterable[Mapping[str, Any]], query: str, *, cap: int,
) -> list[dict[str, Any]]:
    """Case-insensitive substring search over real prompt/assistant turns.

    Pure and testable with synthetic records (F1 — ``crr recall``): reuses
    ``extract_prompt``/``clean_display``/``extract_timestamp`` so the same
    noise skip-list that keeps cards honest also keeps recall from surfacing
    tool-result/meta garbage. Each match carries its chronological ``index``
    (position in ``records``) so a caller can order results (e.g.
    most-recent-first) without re-deriving recency from scratch.

    Snippet context (N adjacent turns around a match) is deferred by
    design — ``-C``/``context`` isn't wired in this slice, so the param was
    dropped rather than kept as an accepted-but-no-op knob. Reintroduce it
    when a caller actually implements the widening.
    """
    query_lower = query.lower()
    matches: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        prompt = extract_prompt(record)
        if prompt is not None:
            if query_lower in prompt.lower():
                matches.append({
                    "role": "user",
                    "text": clean_display(prompt, cap),
                    "index": index,
                    "timestamp": extract_timestamp(record) or "",
                })
            continue
        text = _assistant_text(record)
        if text is not None and query_lower in text.lower():
            matches.append({
                "role": "assistant",
                "text": clean_display(text, cap),
                "index": index,
                "timestamp": extract_timestamp(record) or "",
            })
    return matches


def tail_facts(records: Iterable[Mapping[str, Any]], *, cap: int) -> dict[str, str]:
    """Most recent real prompt + model + activity timestamp, in one pass.

    The three facts all live near the tail, so a single reverse walk fills
    them and stops as soon as they are found (the adapter mirrors this to
    turn what would be several backward file reads into one). ``last_active``
    is the ISO ``timestamp`` of the newest record that carries one — not
    necessarily the record that supplies the prompt/model. Each field is an
    honest ``""`` when absent — never fabricated.
    """
    prompt = model = last_active = ""
    for record in reversed(list(records)):
        if not prompt:
            found = extract_prompt(record)
            if found is not None:
                prompt = clean_display(found, cap)
        if not model:
            found = extract_model(record)
            if found is not None:
                model = found
        if not last_active:
            found = extract_timestamp(record)
            if found is not None:
                last_active = found
        if prompt and model and last_active:
            break
    return {"last_prompt": prompt, "model": model, "last_active": last_active}
