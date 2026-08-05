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

from crr.core import contracts, discovery, transcript
from crr.core.config import DEFAULTS


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
    than raising. Used to guess/verify a resume sid; also consulted from
    status assembly's lock-free pre-scan (``_guessed_upgradable``), but only
    while a `guessed` entry remains unconfirmed — cheap in the common case.
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


def _decode_project_dir_name(name: str) -> str:
    """Best-effort decode of a project dir name back to a cwd.

    ``_project_dir_name`` encodes a cwd by replacing every ``/`` with
    ``-``. That's LOSSY going the other way: a literal ``-`` inside a real
    path component (e.g. this very repo's directory,
    ``Claude-Remote-Rescue``) is indistinguishable from an encoded ``/`` —
    ``"-home-u-Claude-Remote-Rescue".replace("-", "/")`` yields
    ``/home/u/Claude/Remote/Rescue``, not the real path. This is therefore
    a DISPLAY/fallback value, not authoritative: discovery prefers the cwd
    a transcript's own records carry (see ``read_cwd``) and only falls back
    to this decode when that read comes up empty (e.g. an empty
    transcript). Note the round-trip back through ``_project_dir_name`` IS
    stable (every ``-`` becomes a ``/`` here, then every ``/`` becomes a
    ``-`` again there), so even a "wrong" decode still globs the correct
    project directory — it's specifically cwd-as-*meaning* (display, or
    passing it to a real filesystem `cwd=` spawn) that the lossiness
    breaks, not cwd-as-*glob-key*.
    """
    return name.replace("-", "/")


def list_all_transcripts(home: Path | None = None) -> list[dict]:
    """Enumerate every transcript under ``~/.claude/projects/*/*.jsonl``.

    Cheap by design: one glob + one ``stat()`` per file, no content read —
    discovery's on-demand callers (`crr discover`, the lazy
    `/api/discoverable` panel) enrich only the untracked subset afterward
    (via ``read_tail_facts``/``read_cwd``); reading every transcript's
    content here would waste work on the — usually much larger —
    already-journaled majority. Only session-UUID-shaped filenames are
    returned (``contracts.valid_session_id``): downstream, ``sid8``
    derivation, ``ArchiveStore.path_for``, and the ``/api/sid-action``
    UUID gate all assume the shape, so a non-UUID stem would surface as an
    entry nothing can actually adopt.
    """
    home = home or Path.home()
    projects = home / ".claude" / "projects"
    if not projects.is_dir():
        return []
    out = []
    for path in projects.glob("*/*.jsonl"):
        session_id = path.stem
        if not contracts.valid_session_id(session_id):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append({
            "session_id": session_id,
            "cwd": _decode_project_dir_name(path.parent.name),
            "mtime": mtime,
        })
    return out


# Authoritative cwd shows up within the first handful of records in every
# observed transcript (the session-start/snapshot header lines don't carry
# it; the very first real turn does) — bounded so `read_cwd` never has to
# read a multi-megabyte transcript end to end for a field that lives near
# the top.
_CWD_SCAN_LINES = 200


def read_cwd(session_id: str, home: Path | None = None, scan_lines: int = _CWD_SCAN_LINES) -> str | None:
    """The AUTHORITATIVE cwd a transcript's own records carry, or None.

    Unlike ``_decode_project_dir_name`` (lossy: a literal ``-`` in a real
    path component is indistinguishable from an encoded ``/``), this reads
    the ``cwd`` Claude Code actually stamped on the session's turns — the
    source of truth discovery (T-C) needs before handing an adopted entry's
    cwd to anything that spawns with ``cwd=`` (a wrong directory there
    doesn't just display wrong, it fails to revive). On-demand only (never
    the poll path): a forward, line-capped read, distinct from
    ``read_tail_facts``'s backward walk — adding a cwd search to that
    early-exit condition would defeat it for cwd-less transcripts and cost
    a full read on every 5s poll.
    """
    path = find_transcript(session_id, home)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= scan_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                cwd = transcript.extract_cwd(record)
                if cwd is not None:
                    return cwd
    except OSError:
        return None
    return None


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


# Default tail-window bound for the model search (see crr.core.config's
# `model_tail_lines` for the empirical p50/p99 justification — that DEFAULTS
# entry is the injectable prior; this constant only supplies the default
# argument below for callers that don't have a Config to hand).
MODEL_TAIL_LINES = DEFAULTS["model_tail_lines"]
REPLY_TAIL_LINES = DEFAULTS["reply_tail_lines"]


def _read_records(path: Path) -> list[dict]:
    """Parse every JSONL line of ``path`` forward; unparseable lines are
    skipped rather than aborting the read (mirrors read_tail_facts's
    per-line ``json.loads`` guard).

    ``errors="replace"`` mirrors ``_reversed_lines``'s ``.decode("utf-8",
    "replace")``: without it, a transcript with invalid UTF-8 bytes raises
    ``UnicodeDecodeError`` (a ``ValueError`` subclass) during line
    iteration — BEFORE the per-line ``json.loads`` try/except gets a
    chance, and past the caller's ``except OSError`` — turning one corrupt
    file into a traceback that kills the whole search (or, under
    ``--all``, the whole sweep)."""
    records: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return records


# Characters JSON stores ESCAPED (a literal " is written \" on disk), plus
# anything non-printable. A raw-bytes test for a query containing one of
# these could miss a file that really does match once parsed — a silent
# false negative, the one failure mode a prefilter must never have. Such
# queries skip the prefilter and take the full parse path.
_JSON_ESCAPED = set('"\\\n\r\t\b\f')

# Read size for the containment scan. The overlap below must be carried
# between blocks or a term straddling two reads is lost.
_SCAN_BLOCK = 1 << 20  # 1 MiB


def _prefilterable(query: str) -> bool:
    """True if a raw-bytes containment test is SAFE for this query.

    Only plain ASCII with no JSON-escaped or non-printable characters:
    byte-level lowercasing folds ASCII exactly (so case-insensitive
    matching is preserved), and an unescaped literal appears in the file
    verbatim. Anything else falls back to parsing — slower, never wrong.
    """
    return (
        bool(query)
        and query.isascii()
        and query.isprintable()
        and not any(ch in _JSON_ESCAPED for ch in query)
    )


def _file_may_contain(path: Path, query: str) -> bool:
    """Cheap streaming test: could ``path`` possibly contain ``query``?

    Case-insensitive raw-bytes scan in blocks, carrying a ``len(needle)-1``
    overlap so a term split across two reads is still found. Never a false
    negative for a ``_prefilterable`` query; false POSITIVES are harmless
    (the real matcher runs afterward and decides). Unreadable file -> True,
    so an I/O problem degrades to the normal parse path rather than
    silently hiding results.
    """
    needle = query.lower().encode("utf-8", "replace")
    if not needle:
        return True
    carry = b""
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(_SCAN_BLOCK)
                if not block:
                    return False
                window = carry + block.lower()
                if needle in window:
                    return True
                carry = window[-(len(needle) - 1):] if len(needle) > 1 else b""
    except OSError:
        return True


def search_transcript(
    session_id: str, query: str, *, cap: int, home: Path | None = None,
) -> list[dict]:
    """Search one session's transcript for ``query`` (F1 — ``crr recall``).

    On-demand command, not the poll path: reads the whole file forward
    (unlike ``read_tail_facts``'s early-exit backward walk — there is no
    "near the tail" shortcut for an arbitrary query) and delegates the
    actual matching to the pure ``core.search``. An absent transcript
    degrades to an empty list, never an error.
    """
    path = find_transcript(session_id, home)
    if path is None:
        return []
    # Skip the parse entirely when the file's raw bytes can't contain the
    # term. Parsing dominates the cost (json.loads per line), and on a real
    # corpus most files can't match — measured 3x faster with FULL coverage.
    if _prefilterable(query) and not _file_may_contain(path, query):
        return []
    try:
        records = _read_records(path)
    except OSError:
        return []
    return transcript.search(records, query, cap=cap)


def search_cwd(cwd: str, query: str, *, cap: int, home: Path | None = None) -> list[dict]:
    """Search every transcript in ``cwd``'s project dir (F1 — ``--all``).

    Each match is tagged with its ``session_id`` so a caller merging
    matches across sessions (e.g. printing most-recent-first) knows which
    transcript it came from.
    """
    matches: list[dict] = []
    for t in list_transcripts(cwd, home):
        for match in search_transcript(t["session_id"], query, cap=cap, home=home):
            match = dict(match)
            match["session_id"] = t["session_id"]
            matches.append(match)
    return matches


def read_takeover_signal(session_id: str, home: Path | None = None) -> dict[str, float | str]:
    """Sample the takeover safety signal for ``session_id`` (`crr adopt
    --takeover`): ``{"mtime": float, "tail_kind": str}``.

    Reads the TAIL FIRST, THEN stats the mtime — the ordering is load-
    bearing, not incidental. If the transcript is appended to between the
    two reads, tail-first pairs the (now slightly stale) tail we already
    read with a FRESH mtime, so the caller's ``seconds_idle = now - mtime``
    comes out small → "still writing, keep waiting" (the safe direction:
    never mistake an in-flight append for a quiet, adoptable session).
    Stat-first would risk the opposite: pairing a stale-quiet mtime read
    before the append with a tail read after it — pairing "was quiet a
    moment ago" with "already changed", overstating idleness.

    The tail is found with the same bounded backward read + per-line
    ``json.loads`` guard as ``read_tail_facts``, classifying each record
    with ``transcript.turn_boundary`` and stopping at the newest record
    whose kind is NOT ``"other"`` (the scan transparently skips past
    ``<synthetic>``/non-turn noise at the tail to the last REAL turn — see
    ``turn_boundary``'s docstring). No non-``"other"`` record found (a
    transcript of pure noise) → ``tail_kind = ""``.

    An absent transcript, or an mtime ``stat()`` that fails, degrades to
    an honest ``0.0`` / ``""`` — never a fabricated value.
    """
    path = find_transcript(session_id, home)
    if path is None:
        return {"mtime": 0.0, "tail_kind": ""}
    tail_kind = ""
    try:
        for line in _reversed_lines(path):
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            kind = transcript.turn_boundary(record)
            if kind != "other":
                tail_kind = kind
                break
    except OSError:
        pass
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {"mtime": mtime, "tail_kind": tail_kind}


def search_all(
    query: str, *, snippet_cap: int, match_cap: int, byte_budget: int,
    home: Path | None = None, exclude_dirs: list[str] | None = None,
    per_session_cap: int | None = None,
) -> dict:
    """Global recall — search EVERY transcript on disk, newest-first, bounded.

    Backs the dashboard's global recall box. Each ``search_transcript`` reads a
    whole file forward, so an unbounded sweep over a machine with multi-MB
    transcripts is real work; this walks ``list_all_transcripts`` sorted by
    mtime DESCENDING (newest conversations first — the ones a recall usually
    wants) and stops once the cumulative on-disk size would cross
    ``byte_budget`` — a backstop for a pathological corpus, NOT the thing that
    decides what you can find: ``0`` means unlimited (the default), because
    the raw-bytes prefilter in ``search_transcript`` already keeps a full
    sweep cheap. The newest transcript is always searched even if it alone
    exceeds a non-zero budget. Returns ``{"matches", "scanned", "skipped"}``: matches
    are tagged with their ``session_id`` and ranked most-recent-first (capped
    to ``match_cap``); ``scanned`` is how many transcripts were searched;
    ``skipped`` is how many newest-first transcripts the budget left unsearched
    (surfaced to the user — no silent truncation). ``exclude_dirs`` drops
    tool-internal transcripts before any of that, mirroring discovery.
    """
    home = home or Path.home()
    transcripts = list_all_transcripts(home)
    if exclude_dirs:
        # Recall sweeps the SAME pool as discovery, so it honors the SAME
        # exclusion list (config baseline + dashboard-managed). Otherwise the
        # byte budget is spent on tool-internal transcripts and one of them
        # can surface as a match — the exact noise discovery filters out.
        transcripts = [
            t for t in transcripts if not discovery.is_excluded(t["cwd"], exclude_dirs)
        ]
    transcripts.sort(key=lambda t: t["mtime"], reverse=True)
    matches: list[dict] = []
    scanned = 0
    skipped = 0
    used = 0
    for i, t in enumerate(transcripts):
        sid = t["session_id"]
        path = find_transcript(sid, home)
        if path is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        # Always scan the newest; then stop once the budget would be exceeded,
        # counting the current + remaining transcripts as skipped.
        if byte_budget > 0 and scanned > 0 and used + size > byte_budget:
            skipped = len(transcripts) - i
            break
        used += size
        scanned += 1
        for m in search_transcript(sid, query, cap=snippet_cap, home=home):
            m = dict(m)
            m["session_id"] = sid
            matches.append(m)
    return {
        "matches": transcript.rank_matches(
            matches, limit=match_cap, per_session=per_session_cap or None),
        "scanned": scanned,
        "skipped": skipped,
    }


def read_tail_facts(
    session_id: str, cap: int, home: Path | None = None,
    model_tail_lines: int = MODEL_TAIL_LINES,
    reply_tail_lines: int = REPLY_TAIL_LINES,
) -> dict[str, str | int]:
    """Most recent real prompt + model + activity + size, in ONE backward read.

    ``last_prompt``, ``model``, and ``last_active`` all live near the tail, so
    a single reverse read fills them and early-exits — collapsing what would
    be several independent reads (each paying ``find_transcript``'s
    cross-project glob and the first 64KB block) into one on the poll path.
    ``last_active`` is the first timestamped record seen on the backward walk
    (that record IS the newest). ``transcript_bytes`` is a single ``stat()``
    on the already-located path — free relative to the read. The model
    search is bounded to ``model_tail_lines`` (injectable — see
    crr.core.config's ``model_tail_lines``); the prompt and timestamp
    searches are not. Missing/absent transcript degrades to honest empty
    strings / zero, never a fabricated value.

    ``last_reply`` — claude's answer immediately BEFORE that prompt — is the
    one field that forces the walk to continue past the prompt, and on an
    agentic session everything in between is tool_use/tool_result noise. It
    is therefore bounded by ``reply_tail_lines`` (measured here: the reply
    sat 4-65 records before the prompt), and left an honest "" when it isn't
    found inside that window rather than reading the whole file on a 5s poll.
    """
    facts: dict[str, str | int] = {
        "last_prompt": "", "model": "", "last_active": "",
        "last_reply": "", "transcript_bytes": 0,
    }
    path = find_transcript(session_id, home)
    if path is None:
        return facts
    try:
        facts["transcript_bytes"] = path.stat().st_size
    except OSError:
        return facts
    try:
        for i, line in enumerate(_reversed_lines(path)):
            in_model_window = i < model_tail_lines
            in_reply_window = i < reply_tail_lines
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                record = None
            found_prompt_here = False
            if record is not None and not facts["last_prompt"]:
                prompt = transcript.extract_prompt(record)
                if prompt is not None:
                    facts["last_prompt"] = transcript.clean_display(prompt, cap)
                    found_prompt_here = True
            if record is not None and not facts["model"] and in_model_window:
                model = transcript.extract_model(record)
                if model is not None:
                    facts["model"] = model
            if record is not None and not facts["last_active"]:
                ts = transcript.extract_timestamp(record)
                if ts is not None:
                    facts["last_active"] = ts
            # The reply is the first real assistant text AFTER the prompt on
            # this backward walk (i.e. just before it chronologically);
            # _assistant_text already drops tool_use/thinking/<synthetic>.
            if (record is not None and facts["last_prompt"] and not facts["last_reply"]
                    and not found_prompt_here and in_reply_window):
                reply = transcript._assistant_text(record)
                if reply is not None:
                    facts["last_reply"] = transcript.clean_display_tail(reply, cap)
            # Stop once prompt+timestamp are found, and model and reply are
            # each either found or past their windows.
            if (
                facts["last_prompt"]
                and facts["last_active"]
                and (facts["model"] or not in_model_window)
                and (facts["last_reply"] or not in_reply_window)
            ):
                break
    except OSError:
        return facts
    return facts
