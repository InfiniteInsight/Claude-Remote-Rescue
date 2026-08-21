# Dashboard Reauth Design

**Date:** 2026-08-21
**Status:** Draft
**Motivation:** When Claude Code's OAuth login expires, sessions become
inoperable — the mobile app says "sign in on the computer" and the CLI
shows a red error. crr is blind to this: it can't distinguish auth
expiration from a network outage. The auto-kick watchdog wastes its
attempt budget restarting processes that can't authenticate, and the user
has no way to fix it from the phone.

## Problem

Claude Code authenticates via OAuth tokens stored in
`~/.claude/.credentials.json`. The file contains `expiresAt` and
`refreshTokenExpiresAt` as Unix epoch milliseconds. Claude Code has
built-in token refresh logic (`doRefresh`, `requestOAuthTokenRefresh`),
but it runs per-session — if no session is active when the access token
expires, nobody refreshes it, and eventually the refresh token expires
too. When that happens:

- The mobile app shows "sign in on the computer"
- Claude Code shows a red "login again" banner in the TUI
- The process stays alive but cannot make API calls
- crr sees sessions as LIVE (process alive) or CRASHED (if the process
  eventually exits), but has no idea the cause is auth
- The auto-kick watchdog (`_kick_dropped_bridges`) restarts processes
  that immediately fail again, burning through its 3-attempt budget

Re-authentication requires running `claude auth login`, which starts a
localhost OAuth flow. When the browser can't reach localhost (phone
browser, SSH, WSL), Claude Code falls back to displaying a login code
that the user pastes back into the terminal.

## Changes

### 1. Auth state detection (`crr/core/auth.py`)

New pure module. Reads `~/.claude/.credentials.json`, parses the OAuth
timestamps, returns one of four states:

| State | Condition | Dashboard |
|-------|-----------|-----------|
| `VALID` | Both tokens unexpired | No badge |
| `EXPIRING` | Earliest expiration is within 3 days AND refresh token still valid | Yellow warning badge |
| `EXPIRED` | Refresh token expired, OR both tokens expired | Red badge + Reauth button |
| `UNKNOWN` | File missing, malformed, or unreadable | No badge (degrade silently) |

The module also computes `expires_in_seconds` for the badge countdown
text, and takes the credentials path as a constructor argument (injectable
for tests).

**Polling strategy:** crr reads the credentials file on every dashboard
poll cycle (`dashboard_poll_seconds`, default 10s). The file is small
(~1KB) and the read is cheap. Auth state transitions (valid → expiring →
expired) trigger the downstream effects described below. When the file
contents haven't changed (same timestamps), no state transition logic
fires.

**Warning window:** 3 days. The `EXPIRING` state activates when the
earliest expiration is within 3 days. This gives the user advance notice
to sign in at the laptop before anything breaks.

### 2. Watchdog suppression

When auth state is `EXPIRED`, crr sets a global flag that
`_kick_dropped_bridges` checks before each kick decision. While the flag
is set:

- No new kicks are dispatched for any session
- Existing kick history (attempt counts, cooldowns) is preserved
- Dashboard session cards annotate "unreachable" badges with
  "(auth expired)" so the user knows it's a global issue

When auth returns to `VALID` (after successful reauth):

- Flag clears
- Kick counters reset for all sessions (fresh budget via
  `bridge_kicks.KickHistoryStore`)
- Any sessions that crashed during the expired window are auto-reopened
  (via `ops.reopen`)
- Any live sessions that had `remote_control` configured but are
  `unreachable` are kicked (restarted) so they pick up the fresh
  credentials from the file and re-establish their bridges

### 3. Remote reauth flow

Two new API endpoints on the dashboard web server:

**`POST /api/reauth`** — triggers the reauth flow:

1. Spawns `claude auth login` in a dedicated tmux pane named
   `crr-reauth` (isolated from session panes)
2. Polls the pane output (via `tmux capture-pane`) for the OAuth URL
3. Returns the URL in the response body once captured
4. Rejects concurrent attempts (only one reauth at a time)

**`POST /api/reauth-code`** — accepts the login code:

1. Receives the code from the dashboard form submission
2. Pipes it to the `crr-reauth` pane via `tmux send-keys`
3. Watches for success: credentials file updated with fresh timestamps
4. On success: kills the reauth pane, returns success
5. On timeout (5 minutes) or pane exit without credential update:
   cleans up the pane, returns failure, button re-enables

**Security:** Both endpoints are behind the existing loopback + Host
allowlist + JSON Content-Type gate. The login code is a short-lived
one-time token from Anthropic's OAuth server — it has no value after
use. The code is piped directly to the tmux pane and never stored.

### 4. Dashboard UI (`page.html`)

Three states in the header bar (near the existing reachability summary):

**Expiring soon:** Yellow badge — "Login expires in 2d 14h". Tapping
shows an explanatory tooltip. Informational only.

**Expired:** Red badge — "Login expired". A "Reauth" button appears.
Session cards replace per-session "phone: not connected" badges with
"(auth expired)" annotation.

**Reauth in progress:** Amber badge — "Reauth in progress...". A modal
(or inline panel) shows:
1. The auth URL as a tappable link (opens in phone browser)
2. A text input field for the login code
3. A "Submit code" button
4. A "Cancel" button to abort

**Post-reauth success:** Badge clears, brief green flash —
"Login refreshed". Sessions restart in the background; cards update as
they come back online.

`PAGE_VERSION` bumps. No new panels or pages — everything fits in the
existing header and a small reauth modal.

### 5. Status API payload extension

`/api/status` response gains three fields:

| Field | Type | Value |
|-------|------|-------|
| `auth_state` | string | `"valid"`, `"expiring"`, `"expired"`, `"unknown"` |
| `auth_expires_in_seconds` | int \| null | Seconds until earliest expiration, null when unknown |
| `auth_reauth_url` | string \| null | OAuth URL when reauth is in progress, null otherwise |

Added to `contracts.py` with the status payload validation.

## What doesn't change

- The existing auto-kick watchdog logic (cooldown, attempt cap) — only
  suppressed, not replaced
- The crash recovery shim loop in the shell shims
- Session state classification (LIVE/CRASHED/GHOST/PARKED)
- Dashboard session cards layout (only badge text changes)
- The `crr reopen` CLI command
- Tab spawner detection and behavior
- Rescue-check on boot

## Layering

All changes stay within the existing architecture:

- `crr/core/auth.py` is pure core — reads a file, returns an enum.
  No adapter or CLI imports.
- Status payload extension is in `crr/core/contracts.py` (core)
- Watchdog suppression is in `crr/cli.py` (CLI) — reads the flag
  from core, applies it to the existing kick decision
- Reauth endpoints are in `crr/cli.py` (CLI) — they orchestrate
  tmux and file watching, both already used in CLI
- Dashboard UI is in `crr/core/page.html` (core) — rendered by
  `crr/core/web.py`
- No new adapters, no new ports

## Testing

### auth.py (pure, no mocking)
- Valid credentials → `VALID`
- Access token expired but refresh token valid → `EXPIRING`
- Both tokens valid but earliest expires within 3 days → `EXPIRING`
- Both expired → `EXPIRED`
- Missing file → `UNKNOWN`
- Malformed JSON / missing fields → `UNKNOWN`
- 3-day warning window boundary (2d 23h = `VALID`, 3d 0h = `EXPIRING`)
- Epoch millisecond parsing

### Watchdog suppression (test_cli.py)
- Auth expired flag set → kicks suppressed
- Auth valid → kicks proceed normally
- Transition expired → valid → kicks resume, counters reset
- Auto-reopen of crashed sessions after reauth
- Auto-kick of live-but-unreachable sessions after reauth

### Reauth flow (test_cli.py)
- `POST /api/reauth` spawns `crr-reauth` tmux pane
- `POST /api/reauth-code` sends keys to pane
- Success: credentials file updated → flag clears, sessions restarted
- Timeout: pane exits without update → cleaned up, button re-enabled
- Concurrent reauth rejected (409)

### Dashboard (test_web.py)
- Status payload includes `auth_state`, `auth_expires_in_seconds`
- Page version bump + hash pin

### Safety
- No test reads real `~/.claude/.credentials.json` — all use fake
  credentials in `tmp_path`
- No test spawns real tmux — all use monkeypatched tmux adapters
- No test runs real `claude auth login`

## Config

No new config keys. The 3-day warning window is a constant in
`auth.py` (not a config-overridable prior) because there's no
user-facing reason to tune it — it's a display threshold, not a
behavioral one. If a config key is ever needed, it follows the
established pattern.

## Scope

This is a focused reliability feature — detection, display, and
remote remediation for a single failure mode (OAuth expiration).
No new modules beyond `auth.py`. No changes to session lifecycle,
reviver, or archive logic. The reauth flow reuses existing
infrastructure (tmux panes, dashboard polling, file watching).
