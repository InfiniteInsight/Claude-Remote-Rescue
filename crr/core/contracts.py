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

# v2 adds skip_permissions to claude sub-object (persist --dangerously-skip-permissions).
JOURNAL_SCHEMA_VERSION = 2
# v3 adds `tmux_session` (nullable per-session field; `detmux` op — 57195a5).
#    Restored 2026-08-08 (#38): this line was DELETED by a later edit rather
#    than superseded, which is how a ledger silently loses its own history.
# v4 adds `last_active` (T-A — true recency) + `context_pressure` (F2 — compaction badge)
# v5 adds `last_reply` — claude's answer preceding the last prompt (ec64798)
# v6 adds `title` + `slug` — session identity for mobile<->dashboard matching (df434fc)
# v7 adds `remote_control` (spec 2026-08-07 — dropped-Remote-Control watchdog, Slice 1)
# v8 adds `autokick` (spec 2026-08-07 — dropped-Remote-Control watchdog, Slice 3)
# v10 adds `adopted` (#40 — a card built from an adopted transcript, whose
# host/shell were never observed) and a `degraded` member to `autokick`
# (#40 — an unreadable settings file is not the same as a user-off switch)
# v9 adds an `unknown` member to BOTH `remote_control` (#33) and
# `context_pressure` (#39). No new key — two enums widen, so a v8 consumer
# reading a v9 payload would reject a value it has no case for. Both encode
# the same correction: a field that could only ever say "no"/"a level" was
# asserting one when the underlying read had not established anything.
# v11 adds the `parked` display state (spec 2026-08-09, Phase 0) — a card
# whose session the reviver restored into a live tmux session. Like v9, no
# new key: `STATES` widens, and a v10 consumer has no case for the member.
# v12 replaces `remote_control`'s enum — the record-counting off/ok/dropped
# gives way to reachable/unreachable sourced from Claude Code's own
# bridgeSessionId — and adds `waiting_for` (spec 2026-08-09, Phases 1-3)
# v13 adds `conflict` (#48) — two live claudes on one conversation
# v14 adds `attached` (#32) — a restored (parked) card the user has already
# reopened, i.e. a tmux client is attached. False (not nullable) when
# detached or when the attached query could not be read, so an unreadable
# tmux state never claims "you are already in this session".
# v15 adds `auth_state`, `auth_expires_in_seconds`, `auth_reauth_url` to the
# sessions PAYLOAD (not the card) — global OAuth auth state for the dashboard.
# v16 adds `skip_permissions` (bool) to the card — whether the session was
# launched with --dangerously-skip-permissions, toggleable from the dashboard.
SESSIONS_CONTRACT_VERSION = 16
# v2 adds the plain-English `summary` list (restored 2026-08-08, #38 — this
#    entry was deleted rather than superseded when v3 landed)
# v3 adds `params` — the generating caps/lookback/timeout
DIAGNOSTICS_CONTRACT_VERSION = 3
ARCHIVE_CONTRACT_VERSION = 1

# --------------------------------------------------------------------------
# The five lazy API payloads (#36). Every one of these shipped unversioned:
# a consumer of `/api/discoverable` depended on whatever shape it happened
# to have that day, and a dropped field would surface as a wrong answer
# downstream rather than an error at the boundary. All start at v1 — the
# shapes are unchanged, only now they are declared.
# --------------------------------------------------------------------------
# v2 adds `cwd_source` (#34) — whether the row's cwd was read from the
# transcript's own records or reconstructed by the lossy project-dir decode
# v3 adds `dup_count` + `dup_members` (#34) — a worktree checkout's untracked
# transcripts collapse into one row (dup_count>1); dup_members carries the
# folded siblings' {session_id, sid8} so the modal can expand and adopt one.
DISCOVERABLE_CONTRACT_VERSION = 3
UNTRACKED_CONTRACT_VERSION = 1
RECALL_CONTRACT_VERSION = 1
EXCLUSIONS_CONTRACT_VERSION = 1
SETTINGS_CONTRACT_VERSION = 1
# The envelope BOTH action endpoints return (#55). One constant, not two:
# `/api/action` and `/api/sid-action` return the same shape and are handled
# adjacently in web.py, so a second copy would only give them room to drift.
# #36 enumerated the five GET panels and missed these two POST results —
# which is how #49 widened them from {ok, message} to {ok, message, degraded}
# with no version to bump and no validator to update.
ACTION_CONTRACT_VERSION = 1
# The launcher's machines panel (#N — Launcher/Machines Panel). Lists the
# tailnet peers running crr, each row's reachability, and which one is this
# machine. Brand new: no prior unversioned shape to backfill.
MACHINES_CONTRACT_VERSION = 1

# --------------------------------------------------------------------------
# The three dashboard-managed STORES (#36). These matter more than the
# payloads above: a served payload breaks visibly against a page of a known
# version, but a file in the state dir is read back by whatever crr is
# installed later — including an older one after a rollback. Each store
# stamps `v` on write; each accepts an unstamped file as legacy v1 (every
# file already on disk predates this) and REFUSES a version from the
# future, degrading rather than half-reading a shape it does not know.
# --------------------------------------------------------------------------
EXCLUSIONS_STORE_VERSION = 1
SETTINGS_STORE_VERSION = 1
TAB_HEALTH_STORE_VERSION = 1
KICKS_STORE_VERSION = 1
# v1: dashboard login — optional passphrase auth gate (spec 2026-08-26; see
# crr.core.dashboard_auth). Brand new store: no prior unversioned shape.
DASHBOARD_AUTH_STORE_VERSION = 1


def store_version_ok(raw: Any, current: int) -> bool:
    """Is this stored mapping's ``v`` one this build can read?

    True for an absent ``v`` (legacy: written before stores were versioned,
    and otherwise the current shape) and for any int ``v <= current``.
    False for a non-int, a bool (an int subclass — same exclusion every
    other numeric field here makes), or a version from the future.

    Shared by all three stores so "what does a version mean" is answered in
    one place rather than three subtly different ones.
    """
    if not isinstance(raw, Mapping):
        return False
    if "v" not in raw:
        return True
    version = raw["v"]
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version <= current

# --------------------------------------------------------------------------
# Enumerations shared across contracts (single source of truth).
# --------------------------------------------------------------------------

HOSTS = ("tab", "tmux", "ssh")
SHELLS = ("zsh", "bash", "fish")
SID_SOURCES = ("injected", "guessed", "verified")
# "parked" (spec 2026-08-09, Phase 0) is a DISPLAY state only — a session
# the reviver restored into a live tmux session. `classify()` still calls
# it CRASHED, which is what `ops.detmux`/`ops.untmux` require; the card
# says what a reader needs instead of what the op guard needs.
STATES = ("live", "ghost", "crashed", "parked")
# Context-pressure level. "unknown" (#39) is emitted when the session's
# model is not in `context_pressure.MODEL_CONTEXT_WINDOWS` — no confirmed
# context window means no denominator, and a level computed against the
# conservative fallback is a claim about a window nobody established.
# Measured rate: "~1 in 3 transcripts carry NO model at all".
CONTEXT_PRESSURE_LEVELS = ("unknown", "ok", "tight", "will-compact")
# Whether this session's phone link is up (spec 2026-08-09, Phases 1-3).
# Sourced from Claude Code's own `bridgeSessionId`, not inferred from
# transcript records. "unknown" is every failure route — a stale or
# recycled pid, a missing field, no state file at all — because an
# unreadable signal must not become a positive claim.
REMOTE_CONTROL_STATES = ("unknown", "reachable", "unreachable")
# The per-session auto-kick toggle's resolved state (spec 2026-08-07,
# Slice 3), from crr.core.settings.autokick_card_state. THREE values, not
# a bool, because the dashboard toggle must distinguish two different
# reasons a session would not be auto-kicked:
#   "on"         - this session would be auto-kicked (global is on, and
#                  this session either opted in or left it unset).
#   "off"        - this session opted out, but the global switch is ON —
#                  the per-session toggle stays live; flipping it back on
#                  works immediately.
#   "global-off" - the GLOBAL hard switch is off, so nothing is
#                  auto-kicked regardless of this session's own value. The
#                  per-session toggle must render DISABLED with this
#                  reason, never a lying "on" it cannot honour.
#   "degraded"   - the dashboard's settings file cannot be read, so the
#                  watchdog fails closed and kicks nothing (#40). The
#                  BEHAVIOUR matches "global-off", but the REASON does not:
#                  the user never turned the switch off, and telling them
#                  they did sends them to a control that will not fix it.
AUTOKICK_STATES = ("on", "off", "global-off", "degraded")
# How a discovered session's cwd was obtained (#34). The same idea as
# `sid_source`, for the other field adoption has to get right:
#   "verified" - read from the transcript's OWN records (authoritative).
#   "decoded"  - reconstructed from the project directory name, which is
#                lossy: `_decode_project_dir_name` cannot tell an encoded
#                `/` from a literal `-`, so `Claude-Remote-Rescue` comes
#                back as `/home/u/Claude/Remote/Rescue`. Fine to display,
#                NOT fine to hand to a spawn — which is why `_adopt`
#                refuses a decoded cwd that is not a real directory.
CWD_SOURCES = ("verified", "decoded")
# The dashboard's OAuth auth state (spec 2026-08-21), from
# `crr.core.auth.auth_state`. "expiring" sits between "valid" and
# "expired" so a reader can act before the refresh token is gone:
#   "valid"    - access token (or refresh token) has more than
#                EXPIRING_WINDOW_SECONDS left.
#   "expiring" - the earliest of the two tokens expires within the
#                window, OR the access token is already expired but
#                the refresh token is still alive (a kick/restart
#                triggers doRefresh in that case).
#   "expired"  - the refresh token itself is gone; nothing left to
#                silently recover with.
#   "unknown"  - credentials are missing or unparseable. Same "unreadable
#                signal must not become a positive claim" rule as
#                REMOTE_CONTROL_STATES above.
AUTH_STATES = ("valid", "expiring", "expired", "unknown")

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
_JOURNAL_CLAUDE_KEYS_V1 = ("session_id", "sid_source", "started")
JOURNAL_CLAUDE_KEYS = ("session_id", "sid_source", "started", "skip_permissions")

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
    "last_reply",
    "title",
    "slug",
    "model",
    "duplicate_group",
    "tmux_session",
    # A restored (parked) session the user has already reopened — a tmux
    # client is attached (#32). False rather than nullable on purpose: an
    # unreadable tmux query (None attached set) must not become a positive
    # "you are already in this" claim. Only ever True on a parked card.
    "attached",
    "updated",
    "last_active",
    "context_pressure",
    "remote_control",
    # What Claude Code reports this session is blocked on ("permission
    # prompt", "input needed"), "" when it is not waiting or nothing was
    # read. Free text from an undocumented state file, never an enum: crr
    # decides nothing on it, it only tells the reader why a session that
    # cannot be reached is also not moving.
    "waiting_for",
    "autokick",
    "adopted",
    # Two entries for this conversation each own a LIVE claude, so two
    # agents are writing to one transcript (#48). NOT `duplicate_group`,
    # which also fires for the benign shell-beside-its-revived-claude pair.
    # False rather than nullable on purpose: an unreadable process probe
    # must not become a positive claim that two agents are fighting, which
    # would tell the reader to kill something on no evidence.
    "conflict",
    "skip_permissions",
)
SESSIONS_PAYLOAD_KEYS = (
    "contract", "sessions",
    "auth_state", "auth_expires_in_seconds", "auth_reauth_url",
)

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
    "detmuxed",  # deprecated: pre-rename spelling; kept so old records still validate
    "untracked",  # ops.detmux/untrack's current archive reason (terminology: detmux -> untrack)
    "ghost-restored",
    "untmuxed",
    # Terminal (#58). `close` arms a flag the shim's repair loop consumes,
    # and the shim's deregister is what actually stops the reviver. A
    # tmux-revived claude has no shim, so the reviver honours the flag
    # itself and archives under this reason — without it, the watchdog
    # revived the conversation the user had just closed.
    "closed",
    # #99: shell SIGHUP fires deregister before claude may have exited.
    # Revivable — the reviver picks these up like superseded-* records.
    "shell-exited",
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
    """Raise ContractError unless ``entry`` is a valid journal entry (v1 or v2)."""
    entry = _require_mapping(entry, "journal entry")
    _require_exact_keys(entry, JOURNAL_KEYS, "journal entry")

    _require_type(entry["v"], int, "journal 'v'")
    if entry["v"] not in (1, JOURNAL_SCHEMA_VERSION):
        raise ContractError(
            f"journal 'v' is {entry['v']}, this build understands 1..{JOURNAL_SCHEMA_VERSION}"
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
        claude_keys = JOURNAL_CLAUDE_KEYS if entry["v"] >= 2 else _JOURNAL_CLAUDE_KEYS_V1
        _require_exact_keys(claude, claude_keys, "journal 'claude'")
        _require_type(claude["session_id"], str, "journal 'claude.session_id'")
        if not valid_session_id(claude["session_id"]):
            raise ContractError("journal 'claude.session_id' must be a UUID")
        _require_enum(claude["sid_source"], SID_SOURCES, "journal 'claude.sid_source'")
        _require_type(claude["started"], str, "journal 'claude.started'")
        if entry["v"] >= 2:
            _require_type(claude["skip_permissions"], bool, "journal 'claude.skip_permissions'")


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
    # (#40) An ADOPTED card reports host/shell as "" — adoption never
    # observed a shell registration, and the journal's schema filler must
    # not be repeated as fact. Conditional rather than adding "" to the
    # enums: a NORMAL card with an empty shell is still a contract error,
    # which is the case worth catching.
    if card["adopted"]:
        for field in ("host", "shell"):
            if card[field] != "":
                raise ContractError(
                    f"session '{field}' must be '' on an adopted card "
                    f"(nothing was observed), got {card[field]!r}"
                )
    else:
        _require_enum(card["host"], HOSTS, "session 'host'")
        _require_enum(card["shell"], SHELLS, "session 'shell'")
    _require_type(card["session_id"], str, "session 'session_id'")
    if not valid_session_id(card["session_id"]):
        raise ContractError("session 'session_id' must be a UUID")
    _require_enum(card["sid_source"], SID_SOURCES, "session 'sid_source'")
    _require_type(card["sid8"], str, "session 'sid8'")
    _require_type(card["last_prompt"], str, "session 'last_prompt'")
    _require_type(card["last_reply"], str, "session 'last_reply'")
    _require_type(card["title"], str, "session 'title'")
    _require_type(card["slug"], str, "session 'slug'")
    _require_type(card["model"], str, "session 'model'")
    _require_type(card["updated"], str, "session 'updated'")
    # duplicate_group is nullable: None (not in a group) or a group id string.
    if card["duplicate_group"] is not None:
        _require_type(card["duplicate_group"], str, "session 'duplicate_group'")
    _require_type(card["conflict"], bool, "session 'conflict'")
    _require_type(card["attached"], bool, "session 'attached'")
    if card["tmux_session"] is not None:
        _require_type(card["tmux_session"], str, "session 'tmux_session'")
    # last_active (T-A): a possibly-empty ISO timestamp string — "" is an
    # honest "no timestamped turn seen yet", not a missing value.
    _require_type(card["last_active"], str, "session 'last_active'")
    _require_enum(card["context_pressure"], CONTEXT_PRESSURE_LEVELS, "session 'context_pressure'")
    _require_enum(card["remote_control"], REMOTE_CONTROL_STATES, "session 'remote_control'")
    _require_type(card["waiting_for"], str, "session 'waiting_for'")
    _require_enum(card["autokick"], AUTOKICK_STATES, "session 'autokick'")
    _require_type(card["adopted"], bool, "session 'adopted'")
    _require_type(card["skip_permissions"], bool, "session 'skip_permissions'")


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

    # Auth state fields (v15) — global, not per-card.
    _require_enum(payload["auth_state"], AUTH_STATES, "/api/sessions 'auth_state'")
    if payload["auth_expires_in_seconds"] is not None:
        _require_type(
            payload["auth_expires_in_seconds"], int,
            "/api/sessions 'auth_expires_in_seconds'",
        )
    if payload["auth_reauth_url"] is not None:
        _require_type(
            payload["auth_reauth_url"], str,
            "/api/sessions 'auth_reauth_url'",
        )


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


# --------------------------------------------------------------------------
# The five lazy API payloads (#36 — run-3 P7). Canonical key lists +
# validators, matching the shape /api/sessions and /api/diagnostics have
# had all along. Exact-key checks in both directions: a DROPPED field is
# the regression these exist to catch, and an unexpected extra one usually
# means a shape changed without its version moving.
# --------------------------------------------------------------------------

DISCOVERABLE_ROW_KEYS = (
    "session_id", "sid8", "cwd", "cwd_source", "last_active",
    "transcript_bytes", "last_prompt", "mtime", "running",
    # (#34) Worktree collapse: dup_count is the group size (1 when this row
    # stands alone); dup_members lists the folded siblings' {session_id, sid8}.
    "dup_count", "dup_members",
)
UNTRACKED_ROW_KEYS = ("session_id", "sid8", "cwd", "archived_at", "last_prompt")
PAGED_PAYLOAD_KEYS = ("contract", "rows", "total", "filtered", "offset", "limit")
RECALL_MATCH_KEYS = ("session_id", "role", "text", "index", "timestamp")
RECALL_PAYLOAD_KEYS = ("contract", "matches", "scanned", "skipped")
EXCLUSIONS_PAYLOAD_KEYS = (
    "contract", "configured", "managed", "config_path", "config_from_file",
)
SETTINGS_PAYLOAD_KEYS = ("contract", "autokick", "resolved", "config_default", "degraded")
ACTION_RESULT_KEYS = ("contract", "ok", "message", "degraded")
MACHINE_ROW_KEYS = ("name", "url", "online", "is_self", "os")
MACHINES_PAYLOAD_KEYS = ("contract", "machines")


def _require_contract(payload: Mapping[str, Any], expected: int, what: str) -> None:
    got = payload["contract"]
    if isinstance(got, bool) or not isinstance(got, int):
        raise ContractError(f"{what} 'contract' must be an int, got {type(got).__name__}")
    if got != expected:
        raise ContractError(
            f"{what} 'contract' is {got}, this build serves {expected}"
        )


def _validate_paged(payload: Any, expected: int, row_keys: tuple[str, ...], what: str) -> None:
    """Shared validator for the two paged, row-bearing panels.

    `/api/discoverable` and `/api/untracked` back the SAME dashboard modal
    and were deliberately built to the same paging shape, so they share a
    validator rather than carrying two copies that could drift apart —
    which is the failure mode this whole file exists to prevent.
    """
    payload = _require_mapping(payload, f"{what} payload")
    _require_exact_keys(payload, PAGED_PAYLOAD_KEYS, f"{what} payload")
    _require_contract(payload, expected, what)
    _require_type(payload["rows"], list, f"{what} 'rows'")
    for field in ("total", "filtered", "offset", "limit"):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{what} '{field}' must be an int, got {type(value).__name__}")
        if value < 0:
            raise ContractError(f"{what} '{field}' must be >= 0, got {value}")
    for row in payload["rows"]:
        row = _require_mapping(row, f"{what} row")
        _require_exact_keys(row, row_keys, f"{what} row")
        if not valid_session_id(row["session_id"]):
            raise ContractError(f"{what} row 'session_id' is not a session id")
        _require_type(row["sid8"], str, f"{what} row 'sid8'")
        _require_type(row["cwd"], str, f"{what} row 'cwd'")
        if "cwd_source" in row_keys:
            _require_enum(row["cwd_source"], CWD_SOURCES, f"{what} row 'cwd_source'")
        _require_type(row["last_prompt"], str, f"{what} row 'last_prompt'")
        if "dup_count" in row_keys:
            count = row["dup_count"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ContractError(f"{what} row 'dup_count' must be an int >= 1")
            _require_type(row["dup_members"], list, f"{what} row 'dup_members'")


def validate_discoverable_payload(payload: Any) -> None:
    _validate_paged(payload, DISCOVERABLE_CONTRACT_VERSION,
                    DISCOVERABLE_ROW_KEYS, "/api/discoverable")


def validate_untracked_payload(payload: Any) -> None:
    _validate_paged(payload, UNTRACKED_CONTRACT_VERSION,
                    UNTRACKED_ROW_KEYS, "/api/untracked")


def validate_recall_payload(payload: Any) -> None:
    """`scanned`/`skipped` are the lineage half of this payload: how many
    transcripts were actually searched, and how many the budget left
    unsearched. They are contracted for that reason — dropping them would
    turn a partial sweep into a silently complete-looking one."""
    payload = _require_mapping(payload, "/api/recall payload")
    _require_exact_keys(payload, RECALL_PAYLOAD_KEYS, "/api/recall payload")
    _require_contract(payload, RECALL_CONTRACT_VERSION, "/api/recall")
    _require_type(payload["matches"], list, "/api/recall 'matches'")
    for field in ("scanned", "skipped"):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"/api/recall '{field}' must be an int, got {type(value).__name__}")
        if value < 0:
            raise ContractError(f"/api/recall '{field}' must be >= 0, got {value}")
    for match in payload["matches"]:
        match = _require_mapping(match, "/api/recall match")
        _require_exact_keys(match, RECALL_MATCH_KEYS, "/api/recall match")
        _require_type(match["role"], str, "/api/recall match 'role'")
        _require_type(match["text"], str, "/api/recall match 'text'")


def validate_exclusions_payload(payload: Any) -> None:
    """`configured` vs `managed` is this payload's provenance split — which
    entries came from the user's own config.toml and which the dashboard
    wrote. Contracted so the two can never be merged into one anonymous
    list, which would lose which ones the web is allowed to edit."""
    payload = _require_mapping(payload, "/api/exclusions payload")
    _require_exact_keys(payload, EXCLUSIONS_PAYLOAD_KEYS, "/api/exclusions payload")
    _require_contract(payload, EXCLUSIONS_CONTRACT_VERSION, "/api/exclusions")
    for field in ("configured", "managed"):
        _require_type(payload[field], list, f"/api/exclusions '{field}'")
        for entry in payload[field]:
            _require_type(entry, str, f"/api/exclusions '{field}' entry")
    _require_type(payload["config_path"], str, "/api/exclusions 'config_path'")
    _require_type(payload["config_from_file"], bool, "/api/exclusions 'config_from_file'")


def validate_action_result(payload: Any) -> None:
    """The result envelope served by /api/action and /api/sid-action (#55).

    `degraded` is contracted rather than optional because it is the whole
    difference between "reopened, and here is your tab" and "reopened, and
    the tab never came" — a client that silently drops it renders a partial
    failure as a plain success, which is the bug #49 existed to fix.
    """
    payload = _require_mapping(payload, "action result")
    _require_exact_keys(payload, ACTION_RESULT_KEYS, "action result")
    _require_contract(payload, ACTION_CONTRACT_VERSION, "action result")
    for field in ("ok", "degraded"):
        _require_type(payload[field], bool, f"action result '{field}'")
    _require_type(payload["message"], str, "action result 'message'")


def validate_settings_payload(payload: Any) -> None:
    """`autokick` is NULLABLE on purpose: None means "never set, falls back
    to config", which is a different state from an explicit False. `degraded`
    is contracted because a Settings modal that cannot say the store is
    unreadable would show a switch that does nothing."""
    payload = _require_mapping(payload, "/api/settings payload")
    _require_exact_keys(payload, SETTINGS_PAYLOAD_KEYS, "/api/settings payload")
    _require_contract(payload, SETTINGS_CONTRACT_VERSION, "/api/settings")
    if payload["autokick"] is not None:
        _require_type(payload["autokick"], bool, "/api/settings 'autokick'")
    for field in ("resolved", "config_default", "degraded"):
        _require_type(payload[field], bool, f"/api/settings '{field}'")


# --------------------------------------------------------------------------
# /api/machines payload (launcher machines panel).
# --------------------------------------------------------------------------

def validate_machines_payload(payload: Any) -> None:
    """Raise ContractError unless ``payload`` is a valid /api/machines body."""
    payload = _require_mapping(payload, "/api/machines payload")
    _require_exact_keys(payload, MACHINES_PAYLOAD_KEYS, "/api/machines payload")
    _require_type(payload["contract"], int, "/api/machines 'contract'")
    if payload["contract"] != MACHINES_CONTRACT_VERSION:
        raise ContractError(
            f"/api/machines 'contract' is {payload['contract']}, "
            f"this build serves {MACHINES_CONTRACT_VERSION}"
        )
    _require_type(payload["machines"], list, "/api/machines 'machines'")
    for row in payload["machines"]:
        row = _require_mapping(row, "/api/machines row")
        _require_exact_keys(row, MACHINE_ROW_KEYS, "/api/machines row")
