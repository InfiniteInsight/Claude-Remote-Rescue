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


def clean_display_tail(text: str, cap: int) -> str:
    """Collapse whitespace and keep the LAST ``cap`` chars, marking the cut.

    Deliberately the mirror image of ``clean_display``'s head cap, and the
    inconsistency is the point: a user's prompt states its subject up front,
    so the head is the useful part; an assistant reply builds to its
    conclusion, so the tail is ("…so the fix is to bump the timeout"). Text
    already within ``cap`` is returned untouched, with no marker.
    """
    flat = " ".join(text.split())
    if len(flat) <= cap:
        return flat
    return "…" + flat[-cap:]


def center_snippet(text: str, query: str, cap: int) -> str:
    """Whitespace-collapse ``text`` and return a ``cap``-wide window that keeps
    the ``query`` match VISIBLE (F1 — ``crr recall``).

    Head-capping (``clean_display``) hides a match that falls past ``cap`` in a
    long turn. This keeps the cheap head form whenever the match is already
    visible there — so short turns and head matches are byte-for-byte identical
    to ``clean_display`` (no spurious reflow) — and only re-centers the window
    on the match, with ``…`` markers for any elided head/tail, when the match
    would otherwise be cut off. Case-insensitive (mirrors ``search``'s own
    matching); a query that somehow isn't present degrades to the head form.
    """
    flat = " ".join(text.split())
    if len(flat) <= cap:
        return flat
    idx = flat.lower().find(query.lower())
    if idx < 0 or idx + len(query) <= cap:
        return flat[:cap]  # absent (shouldn't happen) or already visible in the head
    half = max(0, (cap - len(query)) // 2)
    start = max(0, idx - half)
    end = min(len(flat), start + cap)
    start = max(0, end - cap)  # pull the window back if it ran into the tail
    snippet = flat[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(flat):
        snippet = snippet + "…"
    return snippet


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
    ``extract_prompt``/``extract_timestamp`` so the same noise skip-list that
    keeps cards honest also keeps recall from surfacing tool-result/meta
    garbage, and ``center_snippet`` so a match deep in a long turn stays
    visible instead of being head-capped off. Each match carries its chronological ``index``
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
                    "text": center_snippet(prompt, query, cap),
                    "index": index,
                    "timestamp": extract_timestamp(record) or "",
                })
            continue
        text = _assistant_text(record)
        if text is not None and query_lower in text.lower():
            matches.append({
                "role": "assistant",
                "text": center_snippet(text, query, cap),
                "index": index,
                "timestamp": extract_timestamp(record) or "",
            })
    return matches


def rank_matches(
    matches: list[dict[str, Any]], *, limit: int, per_session: int | None = None,
) -> list[dict[str, Any]]:
    """Order recall matches most-recent-first and truncate to ``limit``.

    ``per_session`` caps how many matches ONE session may contribute, so a
    single chatty transcript cannot fill every slot and hide the session the
    user is actually looking for (found live: a search returned five matches
    all from the newest session). Recency still orders the result; the cap
    only decides who gets a seat. ``None`` disables it.

    Timestamp is the primary key (ISO-8601 strings sort lexically); ``index``
    breaks ties within a single transcript (and orders an untimestamped
    single-transcript search). An untimestamped match (``timestamp == ""``)
    sorts LAST regardless of index — unknown recency is never presented as
    recent. Shared by ``crr recall`` (cli) and the dashboard's recall provider
    (per-session and global), so the two surfaces can't drift on ordering.
    """
    ordered = sorted(
        matches, key=lambda m: (m.get("timestamp", ""), m.get("index", 0)), reverse=True
    )
    if per_session is None:
        return ordered[:limit]
    seen: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for m in ordered:
        sid = str(m.get("session_id", ""))
        if seen.get(sid, 0) >= per_session:
            continue
        seen[sid] = seen.get(sid, 0) + 1
        out.append(m)
        if len(out) >= limit:
            break
    return out


def turn_boundary(record: Mapping[str, Any]) -> str:
    """Classify a single JSONL record as a turn boundary (`crr adopt
    --takeover`'s safety signal).

    Returns exactly one of:
    - ``"assistant-end"`` — a real (non-``<synthetic>``) assistant turn
      whose ``stop_reason`` is ``"end_turn"``. The only kind that means
      "finished, awaiting the user" — the sole safe tail for a takeover.
    - ``"mid-turn"`` — an assistant record with any OTHER ``stop_reason``
      (empirically ``"tool_use"``, even on a record whose only content is
      a ``text``/``thinking`` block — Claude Code stamps that regardless
      of which block actually holds the tool call), or a ``type=="user"``
      record carrying a top-level ``toolUseResult`` key (a tool-result
      turn: Claude Code will continue, this is not a prompt awaiting a
      reply).
    - ``"user-prompt"`` — a real human prompt (mirrors ``extract_prompt``'s
      noise skip-list) that carries no ``toolUseResult``.
    - ``"other"`` — everything else: non-turn record ``type``s
      (``"permission-mode"``, ``"pr-link"``, ``"bridge-session"``, …),
      ``isMeta`` lines, and malformed/non-Mapping input.

    A ``<synthetic>`` assistant record (the API-error/interrupt turns
    Claude Code writes) is treated transparently — always ``"other"``,
    never ``"assistant-end"`` or ``"mid-turn"`` — mirroring how
    ``extract_model``/``_assistant_text`` already skip through it as not a
    real turn. This matters for a caller scanning backward for the newest
    non-``"other"`` record (Task 2's ``read_takeover_signal``): it lets the
    scan skip past an API-error/interrupt record at the tail to the real
    prior turn, so a session parked behind a synthetic record right after
    a genuine assistant ``end_turn`` still surfaces as ``"assistant-end"``
    instead of getting stuck reporting a phantom ``"mid-turn"`` forever.
    Safety against a still-*active* session isn't this function's job —
    it comes from the idle-seconds guard in ``ready_to_take_over``
    (a live session has a recent mtime, so ``seconds_idle`` stays below
    ``idle_window`` regardless of tail kind).
    """
    if not isinstance(record, Mapping):
        return "other"
    rtype = record.get("type")
    if rtype == "assistant":
        message = record.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            return "other"
        if message.get("model") == "<synthetic>":
            return "other"
        if message.get("stop_reason") == "end_turn":
            return "assistant-end"
        return "mid-turn"
    if rtype == "user":
        if "toolUseResult" in record:
            return "mid-turn"
        if extract_prompt(record) is not None:
            return "user-prompt"
        return "other"
    return "other"


def tail_facts(records: Iterable[Mapping[str, Any]], *, cap: int) -> dict[str, str]:
    """Most recent real prompt + model + activity timestamp, in one pass.

    The three facts all live near the tail, so a single reverse walk fills
    them and stops as soon as they are found (the adapter mirrors this to
    turn what would be several backward file reads into one). ``last_active``
    is the ISO ``timestamp`` of the newest record that carries one — not
    necessarily the record that supplies the prompt/model. Each field is an
    honest ``""`` when absent — never fabricated.
    """
    prompt = model = last_active = reply = ""
    seen_prompt = False
    for record in reversed(list(records)):
        # The prompt's OWN record still has to be considered for
        # model/last_active below — skipping the rest of the loop here would
        # drop the newest timestamp (a real regression the suite caught).
        is_prompt_record = False
        if not prompt:
            found = extract_prompt(record)
            if found is not None:
                prompt = clean_display(found, cap)
                seen_prompt = True
                is_prompt_record = True
        if not reply and seen_prompt and not is_prompt_record:
            # First real assistant text encountered after passing the last
            # prompt on this backward walk = the reply that preceded it.
            # ``_assistant_text`` already drops tool_use-only, thinking-only
            # and <synthetic> turns, so only text a human would recognize
            # as the answer qualifies.
            found = _assistant_text(record)
            if found is not None:
                reply = clean_display_tail(found, cap)
        if not model:
            found = extract_model(record)
            if found is not None:
                model = found
        if not last_active:
            found = extract_timestamp(record)
            if found is not None:
                last_active = found
        if prompt and model and last_active and reply:
            break
    return {
        "last_prompt": prompt, "model": model,
        "last_active": last_active, "last_reply": reply,
    }
