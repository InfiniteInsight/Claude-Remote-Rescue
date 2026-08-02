"""Versioned output contracts (audit P7 — Contracted outputs).

Every shape crr stores or serves is pinned here by a version constant, a
canonical key list, and a validator. The tests import these validators;
the web server can run the same validators in a debug mode. This is the
mechanism that makes an old stored/served shape distinguishable from a
current one — ccresume pinned these shapes only behaviorally, so a v0
entry was indistinguishable from a current one.

Pure stdlib, no imports from adapters or cli (this is core).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------
# Version constants. Bump the relevant one whenever a shape changes; the
# bump is the honest signal that consumers must re-check.
# --------------------------------------------------------------------------

JOURNAL_SCHEMA_VERSION = 1
SESSIONS_CONTRACT_VERSION = 3  # v3 adds the per-session nullable tmux_session field
DIAGNOSTICS_CONTRACT_VERSION = 3  # v3 adds `params` — the generating caps/lookback/timeout
ARCHIVE_CONTRACT_VERSION = 1

# --------------------------------------------------------------------------
# Enumerations shared across contracts (single source of truth).
# --------------------------------------------------------------------------

HOSTS = ("tab", "tmux", "ssh")
SHELLS = ("zsh", "bash", "fish")
SID_SOURCES = ("injected", "guessed", "verified")
STATES = ("live", "ghost", "crashed")

# --------------------------------------------------------------------------
# Canonical key lists.
# --------------------------------------------------------------------------

JOURNAL_KEYS = (
    "v",
    "pid",
    "boot_id",
    "cwd",
    "host",
    "shell",
    "claude",
    "last_cmd",
    "tmux_session",
    "revive_strikes",
    "updated",
)
JOURNAL_CLAUDE_KEYS = ("session_id", "sid_source", "started")

SESSION_CARD_KEYS = (
    "pid",
    "state",
    "cwd",
    "shell",
    "host",
    "session_id",
    "sid_source",
    "sid8",
    "last_prompt",
    "model",
    "duplicate_group",
    "tmux_session",
    "updated",
)
SESSIONS_PAYLOAD_KEYS = ("contract", "sessions")

DIAGNOSTICS_PAYLOAD_KEYS = (
    "contract",
    "source",
    "summary",
    "boots",
    "prev_boot_errors",
    "host_events",
    "degraded",
    "params",
)

# Archive record (audit P8 — State-first lineage): why/when a revival-bearing
# entry left the active set, with the entry preserved verbatim.
ARCHIVE_RECORD_KEYS = ("v", "reason", "archived_at", "entry")
ARCHIVE_REASONS = (
    "superseded-on-register",
    "superseded-on-launch",
    "gave-up",
    "dismissed",
    "detmuxed",
    "ghost-restored",
    "untmuxed",
)


class ContractError(ValueError):
    """A value does not conform to its versioned contract."""


# --------------------------------------------------------------------------
# Session-id shape (audit 2026-07-29 — path-traversal + glob injection).
# --------------------------------------------------------------------------

_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def valid_session_id(sid: Any) -> bool:
    """True iff ``sid`` is a claude session UUID. Everything crr journals is
    one (injected uuid4 / transcript filename stems), so anything else
    reaching a path or glob is an injection, not a session.

    ``fullmatch`` (not ``match``) is deliberate: ``$`` in a plain ``match``
    also matches just before a trailing newline, so ``match`` alone would
    accept ``"<uuid>\\n"`` — a shape-pin regex with that hole would defeat
    the point of this task.
    """
    return isinstance(sid, str) and bool(_SESSION_ID_RE.fullmatch(sid))


# --------------------------------------------------------------------------
# Small internal helpers (kept private; the public surface is the
# validators + constants only).
# --------------------------------------------------------------------------

def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _require_exact_keys(value: Mapping[str, Any], keys: Iterable[str], what: str) -> None:
    expected = set(keys)
    actual = set(value.keys())
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ContractError(f"{what} missing key(s): {sorted(missing)}")
    if unknown:
        raise ContractError(f"{what} has unknown key(s): {sorted(unknown)}")


def _require_type(value: Any, typ: type | tuple[type, ...], what: str) -> None:
    # bool is a subclass of int; reject it where a real int is required.
    if typ is int and isinstance(value, bool):
        raise ContractError(f"{what} must be int, got bool")
    if not isinstance(value, typ):
        names = typ.__name__ if isinstance(typ, type) else "/".join(t.__name__ for t in typ)
        raise ContractError(f"{what} must be {names}, got {type(value).__name__}")


def _require_enum(value: Any, allowed: Iterable[str], what: str) -> None:
    if value not in allowed:
        raise ContractError(f"{what} must be one of {tuple(allowed)}, got {value!r}")


# --------------------------------------------------------------------------
# Journal schema v1 (stored state: state_dir/tabs/<pid>.json).
# --------------------------------------------------------------------------

def validate_journal_entry(entry: Any) -> None:
    """Raise ContractError unless ``entry`` is a valid schema-v1 journal entry."""
    entry = _require_mapping(entry, "journal entry")
    _require_exact_keys(entry, JOURNAL_KEYS, "journal entry")

    _require_type(entry["v"], int, "journal 'v'")
    if entry["v"] != JOURNAL_SCHEMA_VERSION:
        raise ContractError(
            f"journal 'v' is {entry['v']}, this build understands {JOURNAL_SCHEMA_VERSION}"
        )

    _require_type(entry["pid"], int, "journal 'pid'")
    _require_type(entry["boot_id"], str, "journal 'boot_id'")
    _require_type(entry["cwd"], str, "journal 'cwd'")
    _require_enum(entry["host"], HOSTS, "journal 'host'")
    _require_enum(entry["shell"], SHELLS, "journal 'shell'")
    _require_type(entry["last_cmd"], str, "journal 'last_cmd'")
    _require_type(entry["updated"], str, "journal 'updated'")
    _require_type(entry["revive_strikes"], int, "journal 'revive_strikes'")
    # tmux_session is nullable string.
    if entry["tmux_session"] is not None:
        _require_type(entry["tmux_session"], str, "journal 'tmux_session'")

    # claude is nullable: a shell registers at start, before any claude
    # session exists (None). Once present it must be fully formed.
    if entry["claude"] is not None:
        claude = _require_mapping(entry["claude"], "journal 'claude'")
        _require_exact_keys(claude, JOURNAL_CLAUDE_KEYS, "journal 'claude'")
        _require_type(claude["session_id"], str, "journal 'claude.session_id'")
        if not valid_session_id(claude["session_id"]):
            raise ContractError("journal 'claude.session_id' must be a UUID")
        _require_enum(claude["sid_source"], SID_SOURCES, "journal 'claude.sid_source'")
        _require_type(claude["started"], str, "journal 'claude.started'")


# --------------------------------------------------------------------------
# /api/sessions payload.
# --------------------------------------------------------------------------

def validate_session_card(card: Any) -> None:
    """Raise ContractError unless ``card`` is a valid session card."""
    card = _require_mapping(card, "session card")
    _require_exact_keys(card, SESSION_CARD_KEYS, "session card")

    _require_type(card["pid"], int, "session 'pid'")
    _require_enum(card["state"], STATES, "session 'state'")
    _require_type(card["cwd"], str, "session 'cwd'")
    _require_enum(card["shell"], SHELLS, "session 'shell'")
    _require_enum(card["host"], HOSTS, "session 'host'")
    _require_type(card["session_id"], str, "session 'session_id'")
    if not valid_session_id(card["session_id"]):
        raise ContractError("session 'session_id' must be a UUID")
    _require_enum(card["sid_source"], SID_SOURCES, "session 'sid_source'")
    _require_type(card["sid8"], str, "session 'sid8'")
    _require_type(card["last_prompt"], str, "session 'last_prompt'")
    _require_type(card["model"], str, "session 'model'")
    _require_type(card["updated"], str, "session 'updated'")
    # duplicate_group is nullable: None (not in a group) or a group id string.
    if card["duplicate_group"] is not None:
        _require_type(card["duplicate_group"], str, "session 'duplicate_group'")
    if card["tmux_session"] is not None:
        _require_type(card["tmux_session"], str, "session 'tmux_session'")


def validate_sessions_payload(payload: Any) -> None:
    """Raise ContractError unless ``payload`` is a valid /api/sessions body."""
    payload = _require_mapping(payload, "/api/sessions payload")
    _require_exact_keys(payload, SESSIONS_PAYLOAD_KEYS, "/api/sessions payload")

    _require_type(payload["contract"], int, "/api/sessions 'contract'")
    if payload["contract"] != SESSIONS_CONTRACT_VERSION:
        raise ContractError(
            f"/api/sessions 'contract' is {payload['contract']}, "
            f"this build serves {SESSIONS_CONTRACT_VERSION}"
        )

    _require_type(payload["sessions"], list, "/api/sessions 'sessions'")
    for card in payload["sessions"]:
        validate_session_card(card)


# --------------------------------------------------------------------------
# /api/diagnostics payload.
# --------------------------------------------------------------------------

def validate_diagnostics_payload(payload: Any) -> None:
    """Raise ContractError unless ``payload`` is a valid /api/diagnostics body."""
    payload = _require_mapping(payload, "/api/diagnostics payload")
    _require_exact_keys(payload, DIAGNOSTICS_PAYLOAD_KEYS, "/api/diagnostics payload")

    _require_type(payload["contract"], int, "/api/diagnostics 'contract'")
    if payload["contract"] != DIAGNOSTICS_CONTRACT_VERSION:
        raise ContractError(
            f"/api/diagnostics 'contract' is {payload['contract']}, "
            f"this build serves {DIAGNOSTICS_CONTRACT_VERSION}"
        )

    _require_type(payload["source"], str, "/api/diagnostics 'source'")
    _require_type(payload["summary"], list, "/api/diagnostics 'summary'")
    _require_type(payload["boots"], list, "/api/diagnostics 'boots'")
    _require_type(payload["prev_boot_errors"], list, "/api/diagnostics 'prev_boot_errors'")
    _require_type(payload["host_events"], list, "/api/diagnostics 'host_events'")
    _require_type(payload["degraded"], list, "/api/diagnostics 'degraded'")
    _require_diagnostics_params(payload["params"])


def _require_diagnostics_params(value: Any) -> None:
    """`params` records the generating caps/lookback/timeout (audit P3/P5):
    the values a source actually queried with, so the payload is
    regenerable/judgeable later instead of losing that lineage the moment
    ``collect()`` returns. A mapping of str keys to scalar (str/int/float)
    values — bool excluded (an int subclass) for the same reason every
    other int field here rejects it.
    """
    value = _require_mapping(value, "/api/diagnostics 'params'")
    for key, val in value.items():
        if not isinstance(key, str):
            raise ContractError(f"/api/diagnostics 'params' key must be str, got {type(key).__name__}")
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            raise ContractError(
                f"/api/diagnostics 'params[{key!r}]' must be str/int/float, got {type(val).__name__}"
            )


# --------------------------------------------------------------------------
# Archive record (audit P8 — State-first lineage).
# --------------------------------------------------------------------------

def validate_archive_record(record: Any) -> None:
    """Raise ContractError unless ``record`` is a valid archive record.

    An archive record preserves a revival-bearing journal entry with the
    lineage of why and when it left the active set. The nested entry must
    itself be a valid journal entry and must carry a claude session —
    archiving a claude-less entry would preserve nothing worth reviving.
    """
    record = _require_mapping(record, "archive record")
    _require_exact_keys(record, ARCHIVE_RECORD_KEYS, "archive record")

    _require_type(record["v"], int, "archive 'v'")
    if record["v"] != ARCHIVE_CONTRACT_VERSION:
        raise ContractError(
            f"archive 'v' is {record['v']}, this build understands {ARCHIVE_CONTRACT_VERSION}"
        )
    _require_enum(record["reason"], ARCHIVE_REASONS, "archive 'reason'")
    _require_type(record["archived_at"], str, "archive 'archived_at'")

    validate_journal_entry(record["entry"])
    if record["entry"]["claude"] is None:
        raise ContractError("archive 'entry' must carry a claude session (nothing to revive otherwise)")
