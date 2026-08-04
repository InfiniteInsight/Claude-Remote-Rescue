"""Discovery — surface transcripts on disk crr hasn't journaled (T-C).

Pure core: ``untracked`` filters a transcript listing against the journaled
session ids and recency-sorts what's left; ``build_adopted_entry`` (plus
``adopted_pid``) builds the synthetic-but-schema-valid journal entry that
``crr discover --adopt`` and the web ``/api/sid-action {op:"adopt"}``
provider write. No filesystem access lives here — the adapter
(``crr.adapters.transcript_source.list_all_transcripts``/``read_cwd``/
``read_tail_facts``) enumerates and reads the transcripts on disk; the
cli/web composition root glues the journal scan and that enumeration into
these pure functions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from crr.core.journal import new_entry

# Sentinel boot id for adopted entries (see build_adopted_entry). A real
# Linux boot_id is a uuid4-hex string read from
# /proc/sys/kernel/random/boot_id; a real macOS one is a bare decimal
# seconds string (kern.boottime). Neither format can ever equal this
# literal, so classify() always sees a boot-identity mismatch and reports
# CRASHED — recoverable via `crr reopen` (or the watchdog's own revive
# pass), but adoption itself never attaches to a live process.
ADOPTED_BOOT_ID = "adopted"


def adopted_pid(session_id: str) -> int:
    """Deterministic placeholder pid for an adopted transcript's journal slot.

    The journal stores one file per pid (``tabs/<pid>.json``), so an
    adopted transcript — which has no real process — still needs a slot of
    its own. Offsetting well above any real Linux pid (``pid_max`` defaults
    to 4194304 and is rarely raised past 2**22) means this value can never
    collide with an actual live process's journal file; deriving it from
    the session id keeps re-adopting the same sid idempotent (same slot,
    not a fresh file every attempt). A collision between two DIFFERENT
    session ids landing in the same 10,000,000-wide bucket is a
    birthday-bound long shot that adoption's manual, on-demand use doesn't
    need to defend further against on its own — the caller additionally
    refuses to clobber a slot that already belongs to a different sid
    (see cli._adopt).
    """
    return 100_000_000 + (int(session_id.replace("-", "")[:8], 16) % 10_000_000)


# `claude.started` for an adopted entry — deliberately NOT the adoption
# moment. `resume.verify_guessed` upgrades a "guessed" sid to "verified"
# only once the sid's transcript shows a write AFTER `started`; every
# still-"guessed" entry costs `_guessed_upgradable`/`_verify_guessed_sids`
# one `list_transcripts()` glob on EVERY status/poll pass. Stamping
# `started` as "now" would mean an adopted-but-still-crashed session (the
# common case: nothing has resumed it yet) never sees new transcript
# activity after that timestamp, so it would stay "guessed" — and keep
# paying that glob — indefinitely. Epoch zero makes any real transcript
# mtime count as "activity since start", so the very next status/poll
# upgrades it and the recurring cost disappears. This is also more honest
# than backdating to "now": adoption never observed the session's real
# start time, so "unknown, earlier than anything we can observe" is a
# truer claim than a fabricated real timestamp would be.
_UNKNOWN_STARTED = "1970-01-01T00:00:00+00:00"


def build_adopted_entry(session_id: str, cwd: str, now: str) -> dict[str, Any]:
    """Build a schema-v1 journal entry for a transcript crr never journaled.

    The "recoverable entry from an external record" T-C shares
    conceptually with F3's retrack: ``sid_source="guessed"`` (crr never saw
    this sid get injected by a shim — see ``_UNKNOWN_STARTED`` for why
    ``claude.started`` is epoch zero rather than the adoption time), no
    ``tmux_session`` (nothing is parked), ``host="tab"``/``shell="bash"``
    placeholders (adoption never observed a real shell registration — any
    enum member the schema accepts would be equally fabricated), and
    ``boot_id=ADOPTED_BOOT_ID`` so the classifier reports CRASHED: the
    entry shows up as a recoverable card, revivable via ``crr reopen``,
    but adoption itself never attaches to a live process.
    """
    return new_entry(
        pid=adopted_pid(session_id),
        cwd=cwd,
        host="tab",
        shell="bash",
        boot_id=ADOPTED_BOOT_ID,
        now=now,
        tmux_session=None,
        claude={"session_id": session_id, "sid_source": "guessed", "started": _UNKNOWN_STARTED},
    )


def _recency_key(transcript: Mapping[str, Any]) -> float:
    """Epoch seconds for most-recent-first sorting.

    Prefers ``last_active`` — an ISO-8601 timestamp pulled from the
    transcript's own records — parsed to a comparable epoch float rather
    than compared as a raw string: two differently-but-both-validly
    serialized ISO timestamps (e.g. a trailing ``Z`` vs ``+00:00``) are not
    reliably string-orderable (page.html's ``recencyMs()`` documents the
    identical lesson for the client-side sort). Falls back to ``mtime``
    (the transcript file's own mtime, already a plain float) when
    ``last_active`` is empty or unparseable — an honest "the conversation's
    last turn is unknown, but the file changed at T" beats sorting a
    recently-touched file dead last. Both absent sorts dead last
    (``-inf``), never fabricated as "now".
    """
    last_active = transcript.get("last_active") or ""
    if last_active:
        try:
            dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = None
        if dt is not None:
            # A transcript timestamp isn't guaranteed to carry a UTC offset
            # (fromisoformat happily parses a naive string); `.timestamp()`
            # on a naive datetime silently interprets it as LOCAL time,
            # which would misorder it against every aware sibling by the
            # host's UTC offset. Pin naive values to UTC explicitly rather
            # than let that ambiguity leak into the sort.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    mtime = transcript.get("mtime")
    if isinstance(mtime, (int, float)) and not isinstance(mtime, bool):
        return float(mtime)
    return float("-inf")


def is_excluded(cwd: str, patterns: Sequence[str]) -> bool:
    """True if ``cwd`` sits under one of the excluded directory names.

    Some tools drive Claude Code themselves and leave transcripts behind that
    are not the user's conversations — on the author's machine, claude-mem's
    ``.claude-mem/observer-sessions`` accounted for 2809 of 2856 discoverable
    rows (98%), burying the real ones. Those directory names are configurable
    (``discover_exclude_dirs``) and filtered out before anything is read.

    Matching normalizes separators on BOTH sides (see ``_norm_sep``) because
    the only cwd available pre-read is the lossy project-dir decode: Claude
    Code encodes both ``/`` and ``.`` as ``-``, so
    ``/home/u/.claude-mem/observer-sessions`` comes back as
    ``/home/u//claude/mem/observer/sessions`` and a literal ``.claude-mem``
    substring test would never match. A blank pattern matches nothing rather
    than everything — an empty config entry must not silently hide the whole
    list.
    """
    haystack = _norm_sep(cwd)
    return any(_norm_sep(p) in haystack for p in patterns if p.strip())


def _norm_sep(text: str) -> str:
    """Lowercase and collapse ``-``/``.``/``/`` to one separator, for matching.

    Discovery filters run BEFORE the transcript read, so the only cwd
    available is the project-dir decode — which turns every ``-`` into ``/``
    (``-home-u-Claude-Remote-Rescue`` -> ``/home/u/Claude/Remote/Rescue``;
    see ``transcript_source._decode_project_dir_name``). A user filtering by
    the real directory name types the hyphens, so a naive substring match
    finds nothing — a live bug for any hyphenated project (this repo
    included). Normalizing both sides makes either spelling match, and is
    harmless for session ids (their hyphens normalize consistently too).

    ``.`` normalizes too, because the encoder maps it to ``-`` as well —
    without that, a dotted directory like ``.claude-mem`` could not be
    matched at all (see ``is_excluded``).
    """
    return text.lower().replace("-", "/").replace(".", "/")


def filter_and_page(
    rows: Sequence[Mapping[str, Any]], *, query: str, offset: int, limit: int
) -> dict[str, Any]:
    """Filter discoverable rows by ``query`` and slice one page out.

    Pure paging//filtering for the dashboard's discoverable modal, kept out of
    the cli so both the shape and the honesty rules are testable. The filter
    matches ``cwd`` or ``session_id`` as a case-insensitive substring —
    deliberately NOT the prompt text, because these two fields are known
    BEFORE the expensive per-transcript read (the caller enriches only the
    page it is about to show); prompt-content search is what ``crr recall``
    is for.

    Reports ``total`` (how many exist at all) separately from ``filtered``
    (how many matched), so a filtered or paged view can never be mistaken for
    "that's all there is" — the same no-silent-caps rule the recall sweep
    follows. An ``offset`` past the end yields an empty page, not an error.
    """
    q = _norm_sep(query.strip())
    if q:
        matched = [
            r for r in rows
            if q in _norm_sep(str(r.get("cwd", "")))
            or q in _norm_sep(str(r.get("session_id", "")))
        ]
    else:
        matched = list(rows)
    start = max(0, offset)
    return {
        "rows": matched[start:start + max(0, limit)],
        "total": len(rows),
        "filtered": len(matched),
        "offset": start,
        "limit": limit,
    }


def untracked(
    journaled_sids: set[str], transcripts: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Transcripts not in ``journaled_sids``, most-recent-first.

    Each input transcript mapping must carry at least ``session_id``; the
    other fields (``cwd``, ``last_active``, ``transcript_bytes``,
    ``last_prompt``, and the recency-fallback ``mtime``) are optional and
    default to an honest "unknown" value when absent rather than a
    fabricated one. Output rows carry exactly
    ``{session_id, sid8, cwd, last_active, transcript_bytes, last_prompt}``
    — ``mtime`` is consumed only as a sort-key fallback, never surfaced (a
    file mtime is not itself a fact about the conversation).
    """
    keyed: list[tuple[float, dict[str, Any]]] = []
    for t in transcripts:
        sid = t["session_id"]
        if sid in journaled_sids:
            continue
        row = {
            "session_id": sid,
            "sid8": sid[:8],
            "cwd": t.get("cwd", ""),
            "last_active": t.get("last_active", ""),
            "transcript_bytes": t.get("transcript_bytes", 0),
            "last_prompt": t.get("last_prompt", ""),
        }
        keyed.append((_recency_key(t), row))
    keyed.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in keyed]
