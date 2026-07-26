"""Resume-sid derivation + verification (audit P3 — confidence + provenance).

The claude() wrapper journals fresh launches with an injected sid. Resume /
continue / picker launches carry no wrapper-generated sid, so their session
id — and how much to trust it — is derived here:

- ``derive_resume_sid`` classifies a launch at journal time. An explicit
  ``--resume <sid>`` is certain (``verified`` when its transcript already
  exists, else a guess-strength claim). ``--continue`` / picker has no sid
  on argv, so the newest transcript in the cwd is the best guess —
  ``guessed``, deliberately uncertain (ccresume shipped two tabs with the
  same laundered sid because it lacked this label).

- ``verify_guessed`` upgrades a ``guessed`` entry to ``verified`` only when
  its OWN transcript shows activity after the session started — the
  strongest available evidence that the guess named the right session. No
  confirming activity leaves it ``guessed`` (silence never confirms), and
  the sid is never rewritten (a wrong rewrite would mislead the reviver).

Pure core: fed a transcript listing (``{session_id, mtime}``) by the
adapter, so it is testable without touching ``~/.claude``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

_Transcripts = Sequence[Mapping[str, Any]]


def derive_resume_sid(explicit_sid: str | None, transcripts: _Transcripts):
    """Return ``(session_id, sid_source)`` for a resume launch, or None.

    None means there is nothing to journal (no explicit sid and no transcript
    to guess from) — the wrapper then passes claude through untracked.
    """
    if explicit_sid:
        known = {t["session_id"] for t in transcripts}
        return explicit_sid, ("verified" if explicit_sid in known else "guessed")
    if not transcripts:
        return None
    newest = max(transcripts, key=lambda t: t["mtime"])
    return newest["session_id"], "guessed"


def _iso_to_epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def verify_guessed(entry: Mapping[str, Any], transcripts: _Transcripts, now: str):
    """Return an updated entry upgraded guessed→verified, or None if unchanged.

    Only a ``guessed`` entry whose own transcript was modified after
    ``claude.started`` is upgraded. Anything else — non-guessed, absent
    transcript, no post-start activity, or an unparseable ``started`` — is
    left exactly as-is.
    """
    claude = entry.get("claude")
    if not claude or claude.get("sid_source") != "guessed":
        return None
    started_epoch = _iso_to_epoch(claude.get("started", ""))
    if started_epoch is None:
        return None
    by_id = {t["session_id"]: t for t in transcripts}
    transcript = by_id.get(claude["session_id"])
    if transcript is None or transcript["mtime"] <= started_epoch:
        return None
    updated = dict(entry)
    updated["claude"] = {**claude, "sid_source": "verified"}
    updated["updated"] = now
    return updated
