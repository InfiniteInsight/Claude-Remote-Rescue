# Dashboard Reauth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect expired Claude Code OAuth tokens, show warning/expired badges on the dashboard, suppress the auto-kick watchdog during auth outages, and offer a full remote reauth flow from the phone.

**Architecture:** Pure auth-state detection in `crr/core/auth.py` (takes parsed credential data + timestamp, returns state enum — no file I/O in core). The file read lives in `crr/cli.py` and is injected into the status assembly. Two new POST endpoints (`/api/reauth`, `/api/reauth-code`) orchestrate `claude auth login` in a tmux pane; their providers are wired in `cli.py`. The dashboard page gets a header badge (3 auth states) and a reauth modal.

**Tech Stack:** Python stdlib only (zero runtime deps). tmux for the reauth pane. Existing `crr/core/web.py` handler pattern for endpoints.

## Global Constraints

- **One-way layering:** `crr.cli` → `crr.adapters` → `crr.core`. Core never imports adapters or cli. Enforced by `.importlinter` + CI.
- **Zero runtime deps.** Web server is stdlib `http` only.
- **TDD:** Tests first, implementation second. Watch the test fail before implementing.
- **Contract versioning:** `SESSIONS_CONTRACT_VERSION` bumps with ledger comment. `PAGE_VERSION` bumps with pin entry in `test_page_version_guard.py`. `CONFIG_DEFAULTS_VERSION` bumps for new config keys.
- **Version ledger:** Every version bump needs a `# vN ...` comment above/near the constant explaining what changed. `tests/test_version_ledger.py` fails on a hole.
- **No real credentials in tests.** All tests use fake credential data in `tmp_path`.
- **No real tmux in tests.** All tmux interactions use monkeypatched seams.
- **Security:** `textContent` for untrusted fields, `setAttribute("href", ...)` for links. JSON Content-Type CSRF gate on POST endpoints.
- **Layering detail for auth.py:** `crr/core/auth.py` is a PURE module. It takes already-parsed credential data (a dict or raw bytes) and a `now` timestamp as arguments. It does NOT open files. The file read happens in `crr/cli.py` and the result is passed in. This mirrors the `reachability.py` pattern: the adapter/CLI reads, core decides.

---

### Task 1: Pure auth state detection (`crr/core/auth.py`)

**Files:**
- Create: `crr/core/auth.py`
- Create: `tests/test_auth.py`

**Interfaces:**
- Consumes: nothing (standalone pure module)
- Produces:
  - `auth_state(credentials: Mapping[str, Any] | None, *, now: float) -> tuple[str, int | None]` — returns `(state, expires_in_seconds)` where `expires_in_seconds` is seconds until earliest expiration (None when unknown)
  - `EXPIRING_WINDOW_SECONDS = 259200` — 3 days in seconds (defined in auth.py; `AUTH_STATES` lives in `contracts.py` per project convention — all shared enums in one place)

- [ ] **Step 1: Write failing tests for auth_state**

```python
# tests/test_auth.py
"""Auth state detection — pure, no file I/O."""

from __future__ import annotations

import pytest

from crr.core import auth


# A fresh epoch reference (arbitrary). Tests express offsets from this.
_NOW = 1_700_000_000.0  # seconds

# 12 hours in ms (access token lifetime observed empirically)
_12H_MS = 12 * 3600 * 1000
# 30 days in ms (refresh token lifetime observed empirically)
_30D_MS = 30 * 24 * 3600 * 1000
# 3 days in seconds (the warning window)
_3D_S = 3 * 24 * 3600


def _creds(*, access_expires_s: float, refresh_expires_s: float) -> dict:
    """Build a minimal credentials dict with expiration offsets from _NOW.

    Positive offset = expires in the future. Negative = already expired.
    Timestamps are Unix epoch MILLISECONDS (Claude Code's format).
    """
    return {
        "expiresAt": int((_NOW + access_expires_s) * 1000),
        "refreshTokenExpiresAt": int((_NOW + refresh_expires_s) * 1000),
    }


class TestAuthState:
    def test_valid_both_tokens_fresh(self):
        creds = _creds(access_expires_s=_12H_MS / 1000, refresh_expires_s=_30D_MS / 1000)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "valid"
        assert expires_in is not None
        assert expires_in > _3D_S

    def test_expiring_within_3_days(self):
        # Access token expires in 2 days, refresh still has 28 days
        creds = _creds(access_expires_s=2 * 86400, refresh_expires_s=28 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expiring"
        assert expires_in == pytest.approx(2 * 86400, abs=1)

    def test_expiring_boundary_exactly_3_days(self):
        # At exactly 3 days, state is EXPIRING (inclusive boundary)
        creds = _creds(access_expires_s=_3D_S, refresh_expires_s=28 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expiring"

    def test_valid_boundary_just_over_3_days(self):
        # 3 days + 1 second is VALID
        creds = _creds(access_expires_s=_3D_S + 1, refresh_expires_s=28 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "valid"

    def test_expired_refresh_token_gone(self):
        # Refresh token expired, access token still alive
        creds = _creds(access_expires_s=3600, refresh_expires_s=-3600)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expired"

    def test_expired_both_tokens_gone(self):
        creds = _creds(access_expires_s=-3600, refresh_expires_s=-7200)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expired"

    def test_expiring_access_expired_refresh_alive(self):
        # Access expired but refresh still valid and within 3 days — EXPIRING
        # (a kick/restart will trigger doRefresh)
        creds = _creds(access_expires_s=-3600, refresh_expires_s=2 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expiring"

    def test_unknown_none_credentials(self):
        state, expires_in = auth.auth_state(None, now=_NOW)
        assert state == "unknown"
        assert expires_in is None

    def test_unknown_empty_dict(self):
        state, expires_in = auth.auth_state({}, now=_NOW)
        assert state == "unknown"
        assert expires_in is None

    def test_unknown_missing_refresh_field(self):
        state, expires_in = auth.auth_state({"expiresAt": 9999999999999}, now=_NOW)
        assert state == "unknown"
        assert expires_in is None

    def test_unknown_non_numeric_values(self):
        state, expires_in = auth.auth_state(
            {"expiresAt": "tomorrow", "refreshTokenExpiresAt": "next week"}, now=_NOW
        )
        assert state == "unknown"
        assert expires_in is None

    def test_unknown_boolean_values_rejected(self):
        # bool is a subclass of int — must be rejected
        state, expires_in = auth.auth_state(
            {"expiresAt": True, "refreshTokenExpiresAt": False}, now=_NOW
        )
        assert state == "unknown"
        assert expires_in is None

    def test_millisecond_precision(self):
        # Timestamps are ms; ensure no off-by-1000 errors
        ms = int((_NOW + 7200) * 1000)  # 2 hours from now, in ms
        creds = {"expiresAt": ms, "refreshTokenExpiresAt": ms + _30D_MS}
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "valid"
        # expires_in should be ~7200, not ~7200000
        assert 7199 <= expires_in <= 7201

    def test_expires_in_is_smallest_positive(self):
        # Access in 1 day, refresh in 10 days — expires_in is 1 day
        creds = _creds(access_expires_s=86400, refresh_expires_s=10 * 86400)
        _, expires_in = auth.auth_state(creds, now=_NOW)
        assert expires_in == pytest.approx(86400, abs=1)


class TestAuthStateConstants:
    def test_enum_tuple(self):
        # AUTH_STATES is defined in contracts.py (per project convention:
        # all shared enums in one place). auth.py re-exports it.
        from crr.core.contracts import AUTH_STATES
        assert AUTH_STATES == ("valid", "expiring", "expired", "unknown")
        assert auth.AUTH_STATES is AUTH_STATES  # re-export, not a copy

    def test_window_is_3_days(self):
        assert auth.EXPIRING_WINDOW_SECONDS == 3 * 24 * 3600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'crr.core.auth'`

- [ ] **Step 3: Write minimal implementation**

```python
# crr/core/auth.py
"""Auth state detection — pure core, no I/O.

Takes parsed credential data and a wall-clock timestamp, returns the
OAuth state and time until expiration. The file read happens in the CLI
layer; this module only decides.

Mirrors ``crr.core.reachability``: an adapter reads, core classifies.
"""

from __future__ import annotations

from typing import Any, Mapping

from crr.core.contracts import AUTH_STATES

EXPIRING_WINDOW_SECONDS = 3 * 24 * 3600  # 3 days


def auth_state(
    credentials: Mapping[str, Any] | None, *, now: float
) -> tuple[str, int | None]:
    """Classify the OAuth auth state from parsed credential timestamps.

    Returns ``(state, expires_in_seconds)`` where state is one of
    ``AUTH_STATES`` and ``expires_in_seconds`` is seconds until the
    earliest expiration (None when the state is ``"unknown"``).

    ``credentials`` is the parsed JSON from ``~/.claude/.credentials.json``.
    Timestamps (``expiresAt``, ``refreshTokenExpiresAt``) are Unix epoch
    **milliseconds**.
    """
    if credentials is None:
        return ("unknown", None)

    access_ms = credentials.get("expiresAt")
    refresh_ms = credentials.get("refreshTokenExpiresAt")

    if not _is_numeric(access_ms) or not _is_numeric(refresh_ms):
        return ("unknown", None)

    access_s = access_ms / 1000
    refresh_s = refresh_ms / 1000
    access_remaining = access_s - now
    refresh_remaining = refresh_s - now

    if refresh_remaining <= 0:
        return ("expired", int(min(access_remaining, refresh_remaining)))

    earliest_remaining = min(access_remaining, refresh_remaining)

    if earliest_remaining <= EXPIRING_WINDOW_SECONDS:
        return ("expiring", int(earliest_remaining))

    return ("valid", int(earliest_remaining))


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_auth.py -v`
Expected: all PASS

- [ ] **Step 5: Verify import-linter**

Run: `lint-imports`
Expected: PASS (auth.py imports nothing from adapters or cli)

- [ ] **Step 6: Commit**

```bash
git add crr/core/auth.py tests/test_auth.py
git commit -m "feat(core): add pure auth state detection (auth.py)"
```

---

### Task 2: Status API payload extension (`contracts.py` + `status.py`)

**Files:**
- Modify: `crr/core/contracts.py:51` (SESSIONS_CONTRACT_VERSION), `:235` (SESSIONS_PAYLOAD_KEYS), `:425` (validate_sessions_payload)
- Modify: `crr/core/status.py:1` (docstring version), `:133` (assemble_sessions signature), `:299` (return statement)
- Modify: `tests/test_version_ledger.py` (docstring version assertion)
- Modify: existing tests that build sessions payloads (test_web.py, test_cli.py)

**Interfaces:**
- Consumes: `auth.AUTH_STATES` from Task 1
- Produces:
  - `SESSIONS_PAYLOAD_KEYS = ("contract", "sessions", "auth_state", "auth_expires_in_seconds", "auth_reauth_url")` — three new top-level fields
  - `SESSIONS_CONTRACT_VERSION = 15` — bumped from 14
  - `assemble_sessions(...)` gains `auth_state: str = "unknown"`, `auth_expires_in_seconds: int | None = None`, `auth_reauth_url: str | None = None` kwargs and includes them in the returned dict

- [ ] **Step 1: Write failing test for contract validation with auth fields**

Add to `tests/test_web.py` (or a new test in `tests/test_contracts.py` if that file exists — otherwise add near existing `validate_sessions_payload` tests):

```python
def test_sessions_payload_requires_auth_fields():
    """v15 payload must include auth_state, auth_expires_in_seconds, auth_reauth_url."""
    from crr.core import contracts

    # A payload missing the auth fields should fail validation
    payload = {"contract": 15, "sessions": []}
    with pytest.raises(contracts.ContractError, match="missing key"):
        contracts.validate_sessions_payload(payload)


def test_sessions_payload_validates_auth_state_enum():
    from crr.core import contracts

    payload = {
        "contract": 15,
        "sessions": [],
        "auth_state": "bogus",
        "auth_expires_in_seconds": None,
        "auth_reauth_url": None,
    }
    with pytest.raises(contracts.ContractError, match="auth_state"):
        contracts.validate_sessions_payload(payload)


def test_sessions_payload_accepts_valid_auth_fields():
    from crr.core import contracts

    for state in ("valid", "expiring", "expired", "unknown"):
        payload = {
            "contract": 15,
            "sessions": [],
            "auth_state": state,
            "auth_expires_in_seconds": 86400 if state != "unknown" else None,
            "auth_reauth_url": None,
        }
        contracts.validate_sessions_payload(payload)  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py::test_sessions_payload_requires_auth_fields -v` (or wherever you put them)
Expected: FAIL — missing keys or wrong version

- [ ] **Step 3: Update contracts.py**

1. Bump `SESSIONS_CONTRACT_VERSION` from 14 to 15 with a ledger comment:
```python
# v15 adds `auth_state`, `auth_expires_in_seconds`, `auth_reauth_url` to the
# sessions PAYLOAD (not the card) — global OAuth auth state for the dashboard.
SESSIONS_CONTRACT_VERSION = 15
```

2. Extend `SESSIONS_PAYLOAD_KEYS`:
```python
SESSIONS_PAYLOAD_KEYS = (
    "contract", "sessions",
    "auth_state", "auth_expires_in_seconds", "auth_reauth_url",
)
```

3. Extend `validate_sessions_payload` to validate the three new fields after the existing sessions validation:
```python
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
```

Note: `AUTH_STATES` is defined in `contracts.py` (this module) — add the tuple near other enum constants:
```python
AUTH_STATES = ("valid", "expiring", "expired", "unknown")
```
`auth.py` imports it from here (not the other way around). This follows the project convention: all shared enums live in `contracts.py`.

- [ ] **Step 4: Update status.py**

1. Update the module docstring version reference from `v14` to `v15`.

2. Add three kwargs to `assemble_sessions`:
```python
def assemble_sessions(
    entries: ...,
    ...
    *,
    ...
    auth_state: str = "unknown",
    auth_expires_in_seconds: int | None = None,
    auth_reauth_url: str | None = None,
) -> dict[str, Any]:
```

3. Update the return statement (line 299):
```python
    return {
        "contract": contracts.SESSIONS_CONTRACT_VERSION,
        "sessions": cards,
        "auth_state": auth_state,
        "auth_expires_in_seconds": auth_expires_in_seconds,
        "auth_reauth_url": auth_reauth_url,
    }
```

- [ ] **Step 5: Fix all existing tests that build sessions payloads**

Every test that constructs a `{"contract": N, "sessions": [...]}` dict or calls `validate_sessions_payload` needs the three new fields added with default values (`"auth_state": "unknown"`, `"auth_expires_in_seconds": None`, `"auth_reauth_url": None`). The contract version in test payloads must be updated from 14 to 15.

Search for `SESSIONS_CONTRACT_VERSION` and `"contract"` references in test files. Also search for mock `provider()` functions in `test_web.py` and `test_cli.py` that return session payloads — they all need the three auth fields in their return value.

- [ ] **Step 6: Update test_version_ledger.py docstring assertion**

The test `test_status_docstring_version_matches_the_shipped_contract` asserts the docstring's version number matches `SESSIONS_CONTRACT_VERSION`. Since you updated the status.py docstring from v14 to v15, this should pass. Verify.

- [ ] **Step 7: Run the full test suite**

Run: `pytest -x -q`
Expected: all PASS

- [ ] **Step 8: Verify import-linter**

Run: `lint-imports`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add crr/core/contracts.py crr/core/status.py tests/
git commit -m "feat(core): extend sessions payload with auth state fields (v15)"
```

---

### Task 3: Watchdog suppression + post-reauth recovery

**Files:**
- Modify: `crr/cli.py:2304` (`_kick_dropped_bridges` — add auth-expired guard)
- Modify: `crr/cli.py:4020` (`provider()` — inject auth state)
- Modify: `tests/test_cli.py` (new tests for watchdog suppression and recovery)

**Interfaces:**
- Consumes:
  - `auth.auth_state(credentials, now=now)` from Task 1
  - `auth.AUTH_STATES` from Task 1
  - `bridge_kicks.KickHistoryStore.reset(sid)` — existing API for counter reset
  - `ops.reopen(...)` — existing API for session reopening
- Produces:
  - `_read_credentials(path: Path) -> dict | None` — file reader in cli.py (reads `~/.claude/.credentials.json`, returns parsed JSON or None on any error)
  - Auth state injected into `provider()` return value via `assemble_sessions` kwargs

The watchdog suppression is a new guard at the TOP of `_kick_dropped_bridges`, before all existing guards. It is the simplest possible gate: if auth state is `"expired"`, print a diagnostic line and return immediately — no kicks dispatched for any session.

The credential file reader (`_read_credentials`) is a small private function in `cli.py` that opens the file, parses JSON, and returns the dict. On any error (missing file, permission denied, malformed JSON), it returns `None`. This is the I/O half of the auth check; the pure classification happens via `auth.auth_state()`.

Post-reauth recovery (kicking unreachable sessions, reopening crashed sessions, resetting kick counters) is deferred to Task 4's endpoint handlers — it fires when the reauth flow completes, not on every poll cycle.

- [ ] **Step 1: Write failing test — watchdog suppressed when auth expired**

This test must replicate the existing `_kick_dropped_bridges` test setup pattern from `test_cli.py` (search for existing tests like `test_kick_dropped_bridges_` to find the fixture/setup pattern: entries with a LIVE session, unreachable bridge, autokick on, eligible per kick_store, idle status, takeover ready). The ONE difference: pass `credentials_path=` pointing to a file with expired refresh token timestamps.

```python
def test_kick_dropped_bridges_suppressed_when_auth_expired(
    tmp_path, boot, probe, monkeypatch
):
    """When the credentials file says EXPIRED, the watchdog must not kick
    anything — a kicked session would immediately fail again, wasting its
    restart budget.
    """
    now = 1_700_000_000.0
    creds_path = tmp_path / ".credentials.json"
    creds_path.write_text(json.dumps({
        "expiresAt": int((now - 3600) * 1000),
        "refreshTokenExpiresAt": int((now - 7200) * 1000),
    }))

    kick_called = []
    def fake_kick(*a, **kw):
        kick_called.append(True)
        return (True, "kicked")

    # Build a LIVE, unreachable, autokick-on, eligible entry
    # (copy the setup pattern from existing _kick_dropped_bridges tests —
    # look for `test_kick_dropped_bridges_` in test_cli.py. The entry needs:
    #   - classify() == LIVE (valid pid, matching boot_id)
    #   - reachability() == "unreachable" (bridge_session_id=None, pid_matched=True)
    #   - autokick resolved True
    #   - kick_eligible True (no prior attempts)
    #   - may_kick True (status="idle")
    #   - ready_to_take_over True (transcript at assistant turn)
    # All of these are already set up in the existing test helpers;
    # replicate that setup here.)

    # Call _kick_dropped_bridges with credentials_path=creds_path
    # and clock=lambda: now

    assert kick_called == [], "kick must NOT fire when auth is expired"
```

- [ ] **Step 2: Write failing test — watchdog proceeds when auth valid**

Same setup as Step 1 but with valid credentials (both tokens expire far in the future). Assert that `kick` IS called — the auth guard does not block when credentials are valid.

```python
def test_kick_dropped_bridges_proceeds_when_auth_valid(
    tmp_path, boot, probe, monkeypatch
):
    now = 1_700_000_000.0
    creds_path = tmp_path / ".credentials.json"
    creds_path.write_text(json.dumps({
        "expiresAt": int((now + 12 * 3600) * 1000),
        "refreshTokenExpiresAt": int((now + 30 * 86400) * 1000),
    }))

    kick_called = []
    def fake_kick(*a, **kw):
        kick_called.append(True)
        return (True, "kicked")

    # Same LIVE/unreachable/eligible setup as Step 1
    # Call _kick_dropped_bridges with credentials_path=creds_path

    assert len(kick_called) == 1, "kick must fire when auth is valid"
```

- [ ] **Step 3: Write failing test — provider includes auth state**

```python
def test_web_provider_includes_auth_state(tmp_path, monkeypatch):
    """The sessions payload from provider() must include the auth fields."""
    now = 1_700_000_000.0
    creds_path = tmp_path / ".credentials.json"
    creds_path.write_text(json.dumps({
        "expiresAt": int((now + 12 * 3600) * 1000),
        "refreshTokenExpiresAt": int((now + 30 * 86400) * 1000),
    }))

    # Monkeypatch _credentials_path to return creds_path
    monkeypatch.setattr("crr.cli._credentials_path", lambda _cfg: creds_path)
    monkeypatch.setattr("time.time", lambda: now)

    # Call provider() (use the same test setup pattern as existing
    # provider tests in test_cli.py — search for "def provider" tests)

    # Assert on the returned payload:
    # assert payload["auth_state"] == "valid"
    # assert isinstance(payload["auth_expires_in_seconds"], int)
    # assert payload["auth_reauth_url"] is None
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_cli.py::test_kick_dropped_bridges_suppressed_when_auth_expired -v`
Expected: FAIL

- [ ] **Step 5: Implement _read_credentials in cli.py**

```python
def _read_credentials(path: Path) -> dict | None:
    """Read and parse the Claude credentials file, or None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
```

Place this near other private helpers in `cli.py` (near the top of the file, after imports).

- [ ] **Step 6: Add credentials_path kwarg to _kick_dropped_bridges**

Add `credentials_path: Path | None = None` as a keyword argument. At the top of the function body, before the existing `if not config.get("remote_control_watch"):` guard, add:

```python
    if credentials_path is not None:
        creds = _read_credentials(credentials_path)
        state, _ = auth.auth_state(creds, now=clock())
        if state == "expired":
            print("crr revive: auth expired — suppressing auto-kicks "
                  "(reauth required before sessions can reconnect)",
                  file=sys.stderr)
            return
```

This is guard 0, before all existing guards. The import `from crr.core import auth` is added at the top of cli.py (core import, respects layering).

- [ ] **Step 7: Wire auth state into provider()**

In the `provider()` closure inside `_cmd_web()`, add auth state computation and pass to `assemble_sessions`:

```python
    def provider() -> dict:
        now = _now()
        ...
        # Auth state from credentials file
        creds = _read_credentials(_credentials_path(config))
        a_state, a_expires = auth.auth_state(creds, now=time.time())

        payload = status.assemble_sessions(
            ...
            auth_state=a_state,
            auth_expires_in_seconds=a_expires,
            auth_reauth_url=None,  # set by reauth endpoint handler
        )
        ...
```

Add `_credentials_path()` as a helper that returns `Path.home() / ".claude" / ".credentials.json"`.

- [ ] **Step 8: Wire credentials_path into _cmd_revive's call to _kick_dropped_bridges**

In `_cmd_revive`, pass `credentials_path=_credentials_path(config)` to the `_kick_dropped_bridges` call. This ensures the systemd timer (which calls `_cmd_revive`) also checks auth state before kicking.

- [ ] **Step 9: Run the full test suite**

Run: `pytest -x -q`
Expected: all PASS

- [ ] **Step 10: Verify import-linter**

Run: `lint-imports`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit -m "feat(cli): suppress auto-kicks when auth expired + inject auth state into provider"
```

---

### Task 4: Reauth API endpoints (`POST /api/reauth`, `POST /api/reauth-code`)

**Files:**
- Modify: `crr/core/web.py:172` (add new endpoint routing in `handle_request`), `:229` (add new provider params to signature)
- Modify: `crr/cli.py` (add `reauth_provider` and `reauth_code_provider` closures in `_cmd_web`, wire to `handle_request`)
- Modify: `tests/test_web.py` (new tests for the two endpoints)
- Modify: `tests/test_cli.py` (new tests for the provider closures)

**Interfaces:**
- Consumes:
  - `auth.auth_state()` from Task 1
  - `bridge_kicks.KickHistoryStore.reset(sid)` — for post-reauth counter reset
  - `ops.reopen(...)` — for post-reauth session recovery
  - tmux subprocess calls (monkeypatched in tests)
- Produces:
  - `reauth_provider: Callable[[], tuple[bool, str, bool]]` — starts the reauth flow, returns `(ok, url_or_message, degraded)`
  - `reauth_code_provider: Callable[[str], tuple[bool, str, bool]]` — submits the login code, returns `(ok, message, degraded)`
  - Two new POST endpoints in `handle_request`

**Spike data from `claude auth login`:**

Running `claude auth login` in a tmux pane produces this output:
```
Opening browser to sign in…
If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=...&response_type=code&redirect_uri=...
Paste code here if prompted >
```

The URL line starts with `If the browser didn't open, visit: ` followed by the full OAuth URL. The code prompt is `Paste code here if prompted > `. The URL regex to capture: `r"visit:\s+(https://\S+)"`.

**Important:** Use `tmux capture-pane -p -J` (the `-J` flag joins wrapped lines). Without `-J`, long URLs wrap at the pane width and the regex captures a truncated, invalid URL. The spike confirmed this issue — the OAuth URL is ~200 chars and wraps in a default 80-column pane.

- [ ] **Step 1: Write failing tests for POST /api/reauth in test_web.py**

```python
def test_post_reauth_calls_provider(self):
    """POST /api/reauth with JSON body calls reauth_provider."""
    called = []
    def reauth():
        called.append(True)
        return (True, "Reauth started — URL will appear on next poll", False)

    resp = _handle(
        "POST", "/api/reauth",
        headers={"Content-Type": "application/json", "Host": "localhost"},
        body=b"{}",
        reauth_provider=reauth,
    )
    assert resp.status == 200
    assert called
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert "started" in body["message"].lower()


def test_post_reauth_requires_json_content_type(self):
    resp = _handle(
        "POST", "/api/reauth",
        headers={"Content-Type": "text/plain", "Host": "localhost"},
        body=b"{}",
        reauth_provider=lambda: (True, "url", False),
    )
    assert resp.status == 415


def test_post_reauth_unavailable_when_no_provider(self):
    resp = _handle(
        "POST", "/api/reauth",
        headers={"Content-Type": "application/json", "Host": "localhost"},
        body=b"{}",
    )
    assert resp.status == 503


def test_post_reauth_code_calls_provider(self):
    called = []
    def reauth_code(code):
        called.append(code)
        return (True, "Login refreshed", False)

    resp = _handle(
        "POST", "/api/reauth-code",
        headers={"Content-Type": "application/json", "Host": "localhost"},
        body=json.dumps({"code": "abc123"}).encode(),
        reauth_code_provider=reauth_code,
    )
    assert resp.status == 200
    assert called == ["abc123"]
    body = json.loads(resp.body)
    assert body["ok"] is True


def test_post_reauth_code_requires_code_field(self):
    resp = _handle(
        "POST", "/api/reauth-code",
        headers={"Content-Type": "application/json", "Host": "localhost"},
        body=json.dumps({"wrong": "field"}).encode(),
        reauth_code_provider=lambda c: (True, "ok", False),
    )
    assert resp.status == 400


def test_post_reauth_code_rejects_non_string_code(self):
    resp = _handle(
        "POST", "/api/reauth-code",
        headers={"Content-Type": "application/json", "Host": "localhost"},
        body=json.dumps({"code": 12345}).encode(),
        reauth_code_provider=lambda c: (True, "ok", False),
    )
    assert resp.status == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py::test_post_reauth_calls_provider -v`
Expected: FAIL — `_handle()` does not accept `reauth_provider`

- [ ] **Step 3: Add endpoint handling in web.py**

Add two new provider parameters to `handle_request`:
```python
    reauth_provider: Callable[[], tuple[bool, str, bool]] | None = None,
    reauth_code_provider: Callable[[str], tuple[bool, str, bool]] | None = None,
```

Add two new POST handlers after the existing `/api/settings` handler (around line 460):

```python
        if path == "/api/reauth":
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            if reauth_provider is None:
                return _plain(503, "reauth unavailable")
            ok, message, degraded = reauth_provider()
            return _json(200 if ok else 409, _action_result(ok, message, degraded))

        if path == "/api/reauth-code":
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            code = data.get("code") if isinstance(data, dict) else None
            if not isinstance(code, str) or not code:
                return _plain(400, 'expected {"code": "<string>"}')
            if reauth_code_provider is None:
                return _plain(503, "reauth unavailable")
            ok, message, degraded = reauth_code_provider(code)
            return _json(200 if ok else 409, _action_result(ok, message, degraded))
```

- [ ] **Step 4: Update _handle test helper to accept new providers**

In `tests/test_web.py`, update the `_handle` helper to pass `reauth_provider` and `reauth_code_provider` through to `handle_request`.

- [ ] **Step 5: Run web tests**

Run: `pytest tests/test_web.py -v -x`
Expected: all PASS

- [ ] **Step 6: Write failing tests for CLI provider closures (test_cli.py)**

Test the `reauth_provider` closure that spawns `claude auth login` in tmux and captures the URL, and the `reauth_code_provider` closure that sends keys to the pane.

```python
def test_reauth_provider_spawns_tmux_session_nonblocking(monkeypatch, tmp_path):
    """reauth_provider spawns 'claude auth login' in crr-reauth session
    and returns immediately (non-blocking). URL is NOT returned here —
    it surfaces via auth_reauth_url on the next dashboard poll."""
    import subprocess
    cmds = []

    def fake_run(args, **kw):
        cmds.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    ok, message, degraded = reauth_provider()

    assert ok is True
    assert "started" in message.lower()
    # Verify tmux new-session was called with "claude auth login"
    new_session_cmds = [c for c in cmds if "new-session" in c]
    assert len(new_session_cmds) == 1
    assert "claude" in new_session_cmds[0]


def test_reauth_url_captured_on_poll(monkeypatch, tmp_path):
    """After reauth_provider starts the pane, the URL is captured on the
    next provider() poll cycle via _poll_reauth_url_once()."""
    import subprocess
    SPIKE_OUTPUT = (
        "Opening browser to sign in…\n"
        "If the browser didn't open, visit: "
        "https://claude.com/cai/oauth/authorize?code=true&client_id=FAKE\n"
        "Paste code here if prompted > \n"
    )

    def fake_run(args, **kw):
        result = subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "capture-pane" in args:
            result.stdout = SPIKE_OUTPUT
        return result

    monkeypatch.setattr("subprocess.run", fake_run)

    # After reauth_provider() succeeds, the next poll captures the URL
    url = _poll_reauth_url_once()
    assert url is not None
    assert "claude.com" in url


def test_reauth_provider_rejects_concurrent(monkeypatch, tmp_path):
    """Only one reauth at a time."""
    # First call succeeds (monkeypatch subprocess.run as above)
    ok1, _, _ = reauth_provider()
    assert ok1 is True

    # Second call while first is active
    ok2, msg2, _ = reauth_provider()
    assert ok2 is False
    assert "already in progress" in msg2.lower()


def test_reauth_code_provider_sends_keys_nonblocking(monkeypatch, tmp_path):
    """reauth_code_provider sends the code to the crr-reauth pane and
    returns immediately.  Success detection (credential file refresh →
    auth_state flip → recovery) happens on the next provider() poll,
    NOT by blocking the HTTP handler."""
    import subprocess
    cmds = []

    def fake_run(args, **kw):
        cmds.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    ok, message, degraded = reauth_code_provider("abc123")

    assert ok is True
    assert "submitted" in message.lower()
    send_cmds = [c for c in cmds if "send-keys" in c]
    assert len(send_cmds) == 1
    assert "abc123" in send_cmds[0]


def test_post_reauth_recovery_resets_counters_and_recovers(monkeypatch, tmp_path):
    """After successful reauth: kick counters reset, crashed sessions
    reopened, live-but-unreachable sessions kicked."""
    reset_sids = []
    reopen_pids = []
    kicked_pids = []

    # Monkeypatch KickHistoryStore.reset to record calls
    monkeypatch.setattr(
        "crr.core.bridge_kicks.KickHistoryStore.reset",
        lambda self, sid: reset_sids.append(sid),
    )
    # Monkeypatch ops.reopen to record calls
    monkeypatch.setattr(
        "crr.core.ops.reopen",
        lambda *a, **kw: reopen_pids.append(a[3]),  # pid arg
    )
    # Monkeypatch _do_kick to record live-unreachable kicks
    monkeypatch.setattr(
        "crr.cli._do_kick",
        lambda entry, *a, **kw: kicked_pids.append(entry["pid"]),
    )

    # Set up store with entries:
    #  - entry A: CRASHED, has claude session → should be reopened
    #  - entry B: LIVE, unreachable, has claude session → should be kicked
    #  - entry C: LIVE, reachable, has claude session → should NOT be kicked
    # (Follow existing test setup patterns from test_cli.py)

    # Trigger _post_reauth_recovery (called from provider() when
    # auth state transitions from expired to valid)

    assert len(reset_sids) >= 3, "all kick counters must be reset"
    assert len(reopen_pids) == 1, "crashed session must be reopened"
    assert len(kicked_pids) == 1, "live-unreachable session must be kicked"
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `pytest tests/test_cli.py::test_reauth_provider_spawns_tmux_pane_and_captures_url -v`
Expected: FAIL

- [ ] **Step 8: Implement provider closures in cli.py (_cmd_web)**

Add inside `_cmd_web`, near `action_provider`:

```python
    _reauth_lock = threading.Lock()
    _reauth_active = False
    _reauth_url: str | None = None

    def reauth_provider() -> tuple[bool, str, bool]:
        """Non-blocking: spawn the tmux pane and return immediately.

        The OAuth URL surfaces via ``auth_reauth_url`` on the next dashboard
        poll cycle (10s), NOT by blocking the HTTP socket.  This matches the
        existing polling model and avoids holding a connection open for 30s+.
        """
        nonlocal _reauth_active, _reauth_url
        with _reauth_lock:
            if _reauth_active:
                return (False, "Reauth already in progress", False)
            _reauth_active = True
            _reauth_url = None
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", "crr-reauth",
                 "claude", "auth", "login"],
                check=False, capture_output=True,
            )
            return (True, "Reauth started — URL will appear on next poll", False)
        except Exception:
            _cleanup_reauth()
            return (False, "Failed to start reauth", False)

    def _poll_reauth_url_once() -> str | None:
        """Single non-blocking capture attempt.  Called from provider() on
        each dashboard poll cycle when _reauth_active is True."""
        import re
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", "crr-reauth", "-p", "-J"],
            capture_output=True, text=True,
        )
        m = re.search(r"visit:\s+(https://\S+)", result.stdout)
        return m.group(1) if m else None

    def reauth_code_provider(code: str) -> tuple[bool, str, bool]:
        """Non-blocking: send keys and return immediately.

        Success detection happens on the next poll: when the credentials
        file is refreshed, ``auth_state`` flips to ``"valid"`` and
        ``_post_reauth_recovery()`` fires from ``provider()``, not here.
        """
        nonlocal _reauth_active, _reauth_url
        if not _reauth_active:
            return (False, "No reauth in progress", False)
        subprocess.run(
            ["tmux", "send-keys", "-t", "crr-reauth", code, "Enter"],
            check=False, capture_output=True,
        )
        return (True, "Code submitted — watching for credential refresh", False)

    _prev_auth_state: str = "unknown"  # track state transitions in provider()

    def _post_reauth_recovery():
        """After a successful reauth: reset kick counters, reopen crashed
        sessions, and kick unreachable live sessions so they pick up fresh
        credentials from the file."""
        kick_store = bridge_kicks.KickHistoryStore(sd)
        entries = store.scan().entries
        for entry in entries:
            if entry.get("claude") is None:
                continue
            sid = entry["claude"]["session_id"]
            kick_store.reset(sid)

        for entry in entries:
            if entry.get("claude") is None:
                continue
            state = classifier.classify(entry, boot, probe)
            if state == classifier.CRASHED:
                with mutation_lock(sd):
                    spawner, tabs_expected = _tab_spawner(config)
                    ops.reopen(store, archive, boot, probe, entry["pid"],
                               _now(), spawner, config)
            elif state == classifier.LIVE:
                # Live but unreachable — kick so it restarts and picks up
                # fresh credentials via loadCredentials at startup.
                reach = reachability.reachability(
                    entry.get("bridge_session_id"),
                    pid_matched=_pid_matched(entry, probe),
                    field_present=entry.get("bridge_session_id") is not None,
                )
                if reach == "unreachable":
                    with mutation_lock(sd):
                        _do_kick(entry, kick_store, config, boot, probe)

    def _cleanup_reauth():
        nonlocal _reauth_active, _reauth_url
        subprocess.run(
            ["tmux", "kill-session", "-t", "crr-reauth"],
            check=False, capture_output=True,
        )
        _reauth_active = False
        _reauth_url = None
```

Wire the non-blocking reauth state into `provider()`:
```python
        # In provider(), after computing auth state:
        # 1. If reauth is active but URL not yet captured, poll once
        if _reauth_active and _reauth_url is None:
            url = _poll_reauth_url_once()
            if url:
                _reauth_url = url

        # 2. If reauth was active and auth just flipped to valid, recovery fires
        if _prev_auth_state == "expired" and a_state in ("valid", "expiring"):
            _post_reauth_recovery()
            _cleanup_reauth()
        _prev_auth_state = a_state

        payload = status.assemble_sessions(
            ...
            auth_reauth_url=_reauth_url,
        )
```

Wire the providers into the `handle_request` call:
```python
    handle_request(
        ...
        reauth_provider=reauth_provider,
        reauth_code_provider=reauth_code_provider,
    )
```

- [ ] **Step 9: Run the full test suite**

Run: `pytest -x -q`
Expected: all PASS

- [ ] **Step 10: Verify import-linter**

Run: `lint-imports`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add crr/core/web.py crr/cli.py tests/test_web.py tests/test_cli.py
git commit -m "feat(cli): add POST /api/reauth + /api/reauth-code endpoints"
```

---

### Task 5: Dashboard UI — auth badge + reauth modal (`page.html`)

**Files:**
- Modify: `crr/core/page.html` (header badge, reauth modal, JS handlers)
- Modify: `crr/core/web.py:44` (bump `PAGE_VERSION`)
- Modify: `tests/test_page_version_guard.py` (add new pin entry)

**Interfaces:**
- Consumes:
  - `auth_state`, `auth_expires_in_seconds`, `auth_reauth_url` from the `/api/sessions` payload (Task 2)
  - `POST /api/reauth` and `POST /api/reauth-code` endpoints (Task 4)
- Produces:
  - Auth badge in the header bar (3 states: expiring/expired/reauth-in-progress)
  - Reauth modal with OAuth URL link + code input + submit/cancel buttons

- [ ] **Step 1: Bump PAGE_VERSION**

In `crr/core/web.py:44`:
```python
PAGE_VERSION = 60  # v60: Auth expiry badge + reauth modal
```

- [ ] **Step 2: Add auth badge CSS to page.html**

Near the existing `.badge` styles (around lines 92-123), add:

```css
.auth-badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }
.auth-expiring { background: #fbbf24; color: #78350f; }
.auth-expired { background: #ef4444; color: #fff; }
.auth-reauth { background: #f59e0b; color: #78350f; }
```

Add dark-mode variants inside `@media (prefers-color-scheme: dark)`:
```css
.auth-expiring { background: #92400e; color: #fbbf24; }
.auth-expired { background: #991b1b; color: #fca5a5; }
.auth-reauth { background: #78350f; color: #fbbf24; }
```

- [ ] **Step 3: Add auth badge HTML in the header bar**

Near the reachability summary (around lines 1165-1174), add a container:

```html
<span id="authBadge"></span>
```

- [ ] **Step 4: Add reauth modal HTML**

After the existing modals, add:

```html
<div id="reauthModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:100; display:none; align-items:center; justify-content:center;">
  <div style="background:var(--bg, #fff); border-radius:8px; padding:20px; max-width:400px; width:90%;">
    <h3 style="margin:0 0 12px;">Re-authenticate Claude Code</h3>
    <p id="reauthStatus">Starting auth flow...</p>
    <p><a id="reauthUrl" href="#" target="_blank" rel="noopener" style="word-break:break-all;"></a></p>
    <div id="reauthCodeSection" style="display:none;">
      <label>Paste the login code:</label>
      <input id="reauthCodeInput" type="text" style="width:100%; padding:6px; margin:8px 0; box-sizing:border-box;" />
      <button id="reauthSubmitBtn" onclick="submitReauthCode()">Submit code</button>
    </div>
    <button onclick="cancelReauth()" style="margin-top:8px;">Cancel</button>
  </div>
</div>
```

- [ ] **Step 5: Add auth badge rendering JS**

In the poll handler (where sessions data is processed), add:

```javascript
function renderAuthBadge(data) {
  var badge = document.getElementById('authBadge');
  if (!badge) return;
  var state = data.auth_state;
  var expires = data.auth_expires_in_seconds;
  var url = data.auth_reauth_url;

  if (state === 'valid' || state === 'unknown') {
    badge.textContent = '';
    badge.className = '';
    // Auto-close reauth modal if auth just became valid
    var modal = document.getElementById('reauthModal');
    if (modal && modal.style.display === 'flex') {
      document.getElementById('reauthStatus').textContent = 'Login refreshed!';
      setTimeout(function() { modal.style.display = 'none'; }, 2000);
    }
    return;
  }

  if (url) {
    badge.className = 'auth-badge auth-reauth';
    badge.textContent = 'Reauth in progress…';
    // Populate modal with URL from poll (non-blocking flow)
    var modal = document.getElementById('reauthModal');
    if (modal && modal.style.display === 'flex') {
      document.getElementById('reauthStatus').textContent = 'Open this link to sign in:';
      var a = document.getElementById('reauthUrl');
      a.textContent = url;
      a.setAttribute('href', url);
      document.getElementById('reauthCodeSection').style.display = 'block';
    }
    return;
  }

  if (state === 'expiring') {
    badge.className = 'auth-badge auth-expiring';
    badge.textContent = 'Login expires in ' + formatDuration(expires);
    return;
  }

  if (state === 'expired') {
    badge.className = 'auth-badge auth-expired';
    badge.innerHTML = '';
    var text = document.createTextNode('Login expired ');
    badge.appendChild(text);
    var btn = document.createElement('button');
    btn.textContent = 'Reauth';
    btn.onclick = startReauth;
    btn.style.cssText = 'margin-left:6px; padding:2px 8px; border-radius:3px; border:1px solid currentColor; background:transparent; color:inherit; cursor:pointer;';
    badge.appendChild(btn);
  }
}

function formatDuration(seconds) {
  if (seconds == null || seconds <= 0) return '—';
  var d = Math.floor(seconds / 86400);
  var h = Math.floor((seconds % 86400) / 3600);
  if (d > 0) return d + 'd ' + h + 'h';
  var m = Math.floor((seconds % 3600) / 60);
  return h + 'h ' + m + 'm';
}
```

Call `renderAuthBadge(data)` inside the existing poll success handler, after the sessions are rendered.

- [ ] **Step 6: Add reauth flow JS functions**

```javascript
function startReauth() {
  var modal = document.getElementById('reauthModal');
  modal.style.display = 'flex';
  document.getElementById('reauthStatus').textContent = 'Starting auth flow…';
  document.getElementById('reauthCodeSection').style.display = 'none';
  document.getElementById('reauthUrl').textContent = '';
  document.getElementById('reauthUrl').removeAttribute('href');

  fetch('/api/reauth', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}'
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.ok) {
      // Non-blocking: URL arrives via auth_reauth_url on next poll.
      document.getElementById('reauthStatus').textContent = 'Waiting for auth URL…';
    } else {
      document.getElementById('reauthStatus').textContent = 'Error: ' + data.message;
    }
  })
  .catch(function() {
    document.getElementById('reauthStatus').textContent = 'Failed to start reauth.';
  });
}

function submitReauthCode() {
  var code = document.getElementById('reauthCodeInput').value.trim();
  if (!code) return;
  document.getElementById('reauthSubmitBtn').disabled = true;
  document.getElementById('reauthStatus').textContent = 'Submitting code…';

  fetch('/api/reauth-code', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code: code})
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.ok) {
      // Non-blocking: success will show when auth_state flips to
      // "valid" on the next poll. Keep modal open with status message.
      document.getElementById('reauthStatus').textContent = 'Code submitted — waiting for login to refresh…';
    } else {
      document.getElementById('reauthStatus').textContent = 'Error: ' + data.message;
      document.getElementById('reauthSubmitBtn').disabled = false;
    }
  })
  .catch(function() {
    document.getElementById('reauthStatus').textContent = 'Failed to submit code.';
    document.getElementById('reauthSubmitBtn').disabled = false;
  });
}

function cancelReauth() {
  document.getElementById('reauthModal').style.display = 'none';
}
```

- [ ] **Step 7: Add page version pin to test_page_version_guard.py**

Compute the SHA256 hash of the modified `page.html` and add a new entry to `PAGE_PINS`:
```python
    60: "<sha256 hash>",
```

- [ ] **Step 8: Run JS syntax check**

Run: `node --check <(python3 -c "..." )` — extract the `<script>` block and run through `node --check`.

Or run the existing CI command that validates page.html JS.

- [ ] **Step 9: Run the full test suite**

Run: `pytest -x -q`
Expected: all PASS

- [ ] **Step 10: Verify import-linter**

Run: `lint-imports`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add crr/core/page.html crr/core/web.py tests/test_page_version_guard.py
git commit -m "feat(page): auth expiry badge + reauth modal (v60)"
```
