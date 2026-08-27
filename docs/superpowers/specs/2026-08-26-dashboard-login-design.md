# Dashboard Login — Optional App-Level Auth Gate

## Goal

Add an optional passphrase-based login to the CRR dashboard so it does not
depend on the network layer (Tailscale, Cloudflare Tunnel) as its sole
authentication boundary. When enabled, every browser session must
authenticate before accessing the dashboard or its API.

## Motivation

The dashboard can restart, kill, and revive Claude Code sessions — it is
effectively remote control of an Anthropic account. Today the tailnet is the
only gate. This is insufficient when:

- Another person on the tailnet (family, coworker) should not have access.
- CRR is exposed beyond the tailnet (Cloudflare Tunnel, port forward, LAN).
- Defense in depth: a network-layer compromise should not immediately grant
  session control.

## Architecture

### New module: `crr/core/dashboard_auth.py`

Pure core (no adapters, no CLI). Responsibilities:

1. **Passphrase hashing** — `hashlib.scrypt` with a 16-byte random salt,
   `n=2**14, r=8, p=1`. Stored as `{"salt": <hex>, "hash": <hex>}`.
   Minimum passphrase length: 8 characters.

2. **Session token creation** — stateless HMAC-SHA256 tokens:
   ```
   base64(token_id | issued_at_unix) . hmac_sha256(token_id | issued_at_unix, signing_secret)
   ```
   `signing_secret` is `secrets.token_bytes(32)`, generated once at first
   passphrase setup, stored in `dashboard_auth.json`.

3. **Session token validation** — recompute the HMAC
   (`hmac.compare_digest` for timing safety), check
   `now - issued_at < dashboard_session_hours * 3600`.

4. **Login attempt rate limiting** — global in-memory counter. After 5
   consecutive failures, impose `min(2^(failures-5), 300)` seconds
   server-side delay (`time.sleep` before responding). Resets on success
   or service restart.

### Storage: `dashboard_auth.json` in the state dir

Same pattern as `settings.json` and `exclusions.json` (JSON in state dir,
atomic write, degrade-to-default on read).

```json
{
  "v": 1,
  "passphrase_hash": "<hex>",
  "passphrase_salt": "<hex>",
  "signing_secret": "<hex>",
  "login_enabled": true,
  "bootstrap_dismissed": false
}
```

State transitions (UPDATED 2026-08-26 — see "Bootstrap prompt — SUPERSEDED"
below for the full revision; the original row for `false`/`false` is struck
through and corrected here so this table doesn't contradict the gate):

| `login_enabled` | `bootstrap_dismissed` | Behavior |
|---|---|---|
| `false` | `false` | ~~Dashboard functional, bootstrap prompt shown (default)~~ **UNDECIDED: blocking setup page, dashboard unreachable, until enable or dismiss** |
| `false` | `true` | OPTED_OUT: dashboard functional, no prompt (user chose "Continue without login") |
| `true` | (ignored) | ENABLED: login required, passphrase set |

**Corrupt file handling (revised, user-decided 2026-08-26):** fail CLOSED,
not open. The original rationale below was rejected: a corrupt auth file
silently disabling the security boundary is unacceptable, even to avoid a
phone-only lockout. Three states, distinguished by
`DashboardAuthStore.is_corrupt()`:

1. File absent (`FileNotFoundError`) → `is_corrupt()` False → login
   disabled, bootstrap prompt as today. Unchanged — this is the
   fresh-install/upgrade path, not a failure.
2. File valid → unchanged.
3. File present but corrupt (invalid JSON, not a dict, or a store version
   this build doesn't understand) → `is_corrupt()` True → the auth gate
   activates: every request is treated as unauthenticated (`GET /` shows
   the login page, APIs 401), no existing session cookie validates, and a
   login attempt returns "Auth store is corrupted — repair or delete
   dashboard_auth.json on the server." instead of "Incorrect passphrase".
   Recovery is deliberately shell-only.

Superseded rationale (kept for context): "degrade to login disabled
(fail-open for access) — a corrupt auth file locking someone out of their
own dashboard from their phone, with no CLI access to fix it, is worse
than a brief open window." Rejected because it conflates a data-integrity
failure with a deliberate off state, and a corrupt file is exactly the
kind of unexpected condition an attacker (or bit rot) could induce to
disable the gate.

### Config key

`dashboard_session_hours: 720` (30 days) in `config.py` DEFAULTS. Bumps
`CONFIG_DEFAULTS_VERSION`. Overridable via `config.toml`.

## Request Flow

UPDATED 2026-08-26 (default-secure bootstrap; step 2 below is superseded —
see "Bootstrap prompt — SUPERSEDED"):

1. Request arrives → host allowlist check (unchanged, always first).
2. If corrupt → treat as ENABLED with a cookie that can never validate
   (step 3, but no login attempt ever succeeds; setup ops also blocked).
3. Else if `login_enabled` is true → check for valid `crr_session` cookie:
   - Valid → proceed to normal routing.
   - Invalid/missing + `GET /` → serve login page (not the dashboard).
   - Invalid/missing + API request → 401 JSON `{"error": "unauthorized"}`.
   - **Exceptions (always unauthenticated):** `GET /api/version` (page
     self-heal), `GET /manifest.webmanifest` (PWA install),
     `POST /api/login` (the login endpoint itself).
4. Else if UNDECIDED (`login_enabled=false AND bootstrap_dismissed=false`)
   → the blocking setup gate: `GET /` serves the setup page; every other
   non-exempt route 401s; `POST /api/dashboard-auth` is reachable
   unauthenticated for `enable`/`dismiss-bootstrap` only.
5. Else (OPTED_OUT: `login_enabled=false AND bootstrap_dismissed=true`) →
   pass through, exactly today's disabled behavior.

## Cookie Attributes

```
Set-Cookie: crr_session=<token>; HttpOnly; SameSite=Strict; Path=/; Max-Age=<session_hours*3600>
```

No `Secure` flag. CRR serves over plain HTTP on loopback — Tailscale Serve
and Cloudflare Tunnel terminate TLS at their edge. A `Secure` cookie on a
plain HTTP `Set-Cookie` is silently dropped by the browser. `SameSite=Strict`
+ `HttpOnly` + the existing host allowlist + JSON content-type gate provide
the real protection.

## UI

### Bootstrap prompt — SUPERSEDED (user decision 2026-08-26)

**Original design (below, kept for history): an advisory modal overlay on
first visit.** Appeared when `login_enabled=false` and
`bootstrap_dismissed=false`. The dashboard was fully functional behind it —
the modal was advisory, not blocking. Re-appeared every visit until the
user clicked "Set passphrase" (→ `login_enabled=true`) or "No login" (→
`bootstrap_dismissed=true`).

**Revised (2026-08-26): default-secure, blocking setup page.** Auth is now
opt-OUT rather than opt-IN. `login_enabled=false AND
bootstrap_dismissed=false` (UNDECIDED) is no longer "dashboard functional,
advisory prompt shown" — it's a hard gate: `GET /` serves a self-contained
blocking setup page (`dashboard_auth.setup_page()`, same family as
`login_page()`) with zero dashboard content, and every API 401s except a
narrow unauthenticated `POST /api/dashboard-auth` restricted to
`{"op": "enable", ...}` and `{"op": "dismiss-bootstrap"}` — `change` and
`disable` are rejected explicitly even here. The dashboard (and its API)
stays unreachable until the first visitor either sets a passphrase or
clicks "Continue without login (not recommended)".

This makes the **first-comer-owns-bootstrap** property explicit rather than
incidental: whoever reaches an undecided dashboard first is the one who
decides whether it gets a passphrase — the same trust model the tailnet
boundary already implied (anyone who can reach the dashboard at all is
already inside it), just no longer hidden behind a dashboard that appeared
to work regardless. `disable()` was also fixed to set
`bootstrap_dismissed=true` (disabling IS opting out) — without that, an
authenticated user turning login off would land back in UNDECIDED and be
immediately re-blocked by the setup gate on their very next request.
`PAGE_VERSION` bumped to 62 to remove the now-dead advisory-modal markup
and JS (`#bootstrapModal`, `checkBootstrap`, `showBootstrapSetup`,
`submitBootstrapPassphrase`, `dismissBootstrap`) — the Settings-modal login
section (`renderLoginSection`) is unaffected, since OPTED_OUT and ENABLED
still need it.

The original "modal overlay" section follows for context; treat every
"dashboard is fully functional behind it" statement as superseded by the
above.

### Login page (standalone, replaces dashboard when unauthenticated)

Served when `login_enabled=true` and no valid cookie. The dashboard content
is never sent to an unauthenticated client.

- Single passphrase field + "Log in" button.
- On success: `Set-Cookie` + redirect to `/`.
- On failure: "Incorrect passphrase" with rate limiting feedback ("too many
  attempts, try again in N seconds").
- Same dark theme as the dashboard, minimal styling.
- Rendered by `dashboard_auth.py`'s `login_page()` function — a small
  self-contained HTML string (not a template in `page.html`), so the
  dashboard's full JS/HTML is never sent to an unauthenticated client.

### Settings modal — new "Dashboard Login" section

Added above the existing auto-kick section in the `#admin-modal`:

- **When login enabled:** "Login is enabled" label, "Change passphrase"
  button (requires current passphrase), "Disable login" button (requires
  current passphrase), "Log out" button.
- **When login disabled:** "Set passphrase" button (same form as bootstrap).

### New endpoints

`POST /api/login` — accepts `{"passphrase": "..."}`. Returns `Set-Cookie`
on success, 401 on failure. Same CSRF posture as every other POST (JSON
content-type gate, host allowlist, no CORS). Unauthenticated by definition.

`POST /api/logout` — clears the cookie. Authenticated only.

`POST /api/dashboard-auth` — authenticated. Settings modal operations:
- `{"op": "enable", "passphrase": "...", "confirm": "..."}`
- `{"op": "change", "current": "...", "new": "...", "confirm": "..."}`
- `{"op": "disable", "current": "..."}`
- `{"op": "dismiss-bootstrap"}`

## Security Properties

### Passphrase invalidation

Changing the passphrase regenerates `signing_secret`. All existing session
cookies become invalid (HMAC won't verify against the new secret). Open
dashboards see the login page on their next request. Clean break.

### Disabling login

Sets `login_enabled=false`. Cookies are simply ignored (auth check skipped).
Re-enabling login generates a new `signing_secret`, so old cookies from the
previous enabled period don't work.

### Rate limiting

- Global in-memory counter (not per-IP — CRR doesn't see the real client
  IP behind Tailscale Serve).
- Server-side delay (`time.sleep`) — cannot be bypassed by the client.
- `ThreadingHTTPServer` handles concurrent requests on other threads during
  the sleep.
- Resets on service restart — acceptable since an attacker who can restart
  the service has shell access.

### What does NOT change

- Host allowlist logic — untouched, still the first check.
- JSON content-type CSRF gate — untouched.
- All endpoint behavior when login is disabled — identical to today.
- Existing Settings modal sections (auto-kick, exclusions) — unchanged.

## Contract Changes

- `DASHBOARD_AUTH_STORE_VERSION = 1` in `contracts.py`.
- `CONFIG_DEFAULTS_VERSION` bumps for `dashboard_session_hours`.
- `PAGE_VERSION` bumps for login page, bootstrap modal, Settings additions
  (v61), then again (v62) to REMOVE the bootstrap modal once it became dead
  code under the blocking-setup-page revision.
- `SESSIONS_PAYLOAD_VERSION` does NOT bump — the shape is unchanged.

## Global Constraints

- Zero runtime dependencies (stdlib only: `hashlib`, `hmac`, `secrets`,
  `time`, `json`).
- One-way layering: `dashboard_auth.py` lives in `crr.core`, selected/wired
  in `crr.cli`.
- TDD: tests first, implementation second.
- `PAGE_VERSION` bump for every `page.html` change.
- `CONFIG_DEFAULTS_VERSION` bump for the new config key.
- `textContent` for untrusted fields, `setAttribute("href", ...)` for links.
- Version ledger: every version bump needs a `# vN ...` comment.

## Testing

### `tests/test_dashboard_auth.py` (pure core)

- Passphrase hash round-trip (hash then verify).
- Wrong passphrase rejected (timing-safe).
- Token creation → validation round-trip.
- Expired token rejected.
- Token invalid after signing secret change (passphrase change invalidation).
- Rate limiter: permits up to 5, blocks on 6th, resets on success.
- Store read/write; corrupt file fails closed (absent file stays not-corrupt).
- Minimum passphrase length enforcement (< 8 chars rejected).

### `tests/test_web.py` (request handler)

- Unauthenticated `GET /` gets login page when login enabled.
- Unauthenticated API request gets 401 when login enabled.
- Valid cookie passes through to normal routing.
- `/api/version` passes through without auth (self-heal exemption).
- Login disabled → no auth check (current behavior preserved).
- `POST /api/login` success sets cookie.
- `POST /api/login` failure returns 401.
- `POST /api/dashboard-auth` operations (enable/change/disable/dismiss).
- UPDATED 2026-08-26: the advisory bootstrap-modal coverage above
  ("prompt shown when not dismissed") is superseded — see
  `TestSetupGate` in `test_web.py` and `TestDashboardLoginStateMachine`
  in `test_cli.py` for the blocking-setup-page state-machine coverage
  (UNDECIDED/OPTED_OUT/ENABLED/CORRUPT) that replaces it.
