# Security Model

Claude-Remote-Rescue exposes a web dashboard that can **restart and revive
processes**, so its security posture is deliberately conservative. This
document is the distilled threat model; the rationale for each decision
lives inline in [DESIGN.md](DESIGN.md).

## Trust boundary

- **The dashboard binds to loopback only** (`127.0.0.1`). It is never bound
  to `0.0.0.0`.
- **The tailnet is the entire authentication boundary.** You reach the
  dashboard by putting it on your [Tailscale](https://tailscale.com) tailnet
  (`tailscale serve`) or behind your own authenticating reverse proxy.
- If you expose the loopback port to a wider network yourself, you have
  removed the boundary crr relies on. Don't.

## Dashboard login (default-pending, opt-out)

crr can additionally gate the dashboard behind a passphrase, on top of the
tailnet boundary above. Auth is **default-pending, not default-off**: a
fresh install serves a blocking setup page — no dashboard content, no API
access — until the first visitor either sets a passphrase or explicitly
declines ("Continue without login"). Declining can be reversed later from
Settings ("Set passphrase"), and enabling can be undone the same way
("Disable login").

This is the same trust model the tailnet boundary already has, made
explicit: **whoever reaches an undecided dashboard first decides** whether
it gets a passphrase. Nothing here defends against a second person already
on the tailnet racing the first visitor to that page — the tailnet
allowlist above is what keeps strangers off it in the first place.

- **Passphrase hashing**: scrypt (`hashlib.scrypt`), unique random salt per
  passphrase, timing-safe comparison (`hmac.compare_digest`).
- **Sessions are stateless**: `crr_session` is an HMAC-SHA256-signed token
  (`token_id | issued_at`, signed with a per-setup random secret), not a
  server-side session table. Cookie flags: `HttpOnly`, `SameSite=Strict`, no
  `Secure` — crr serves plain HTTP on loopback; TLS terminates at the
  Tailscale Serve / tunnel edge, and a `Secure` flag on a plain-HTTP
  `Set-Cookie` is silently dropped by the browser.
- **Logout is stateless too**: `/api/logout` clears the cookie client-side,
  but a captured token remains valid until it expires or the signing secret
  rotates. Changing the passphrase (or disabling then re-enabling login)
  mints a new secret and invalidates every outstanding session at once —
  the actual remedy for a suspected leaked cookie.
- **Three distinct states for `dashboard_auth.json`, each gated
  differently — a *missing* file is not treated as a failure at all**:
  - **Missing** (fresh install, or upgrade from a build predating this
    feature): the blocking setup gate is active. `GET /` serves a
    self-contained setup page (`dashboard_auth.setup_page()`) — no
    dashboard content — and every API returns 401, except a narrow,
    unauthenticated `POST /api/dashboard-auth` that accepts only
    `{"op": "enable", ...}` or `{"op": "dismiss-bootstrap"}`; `change` and
    `disable` are rejected explicitly even here, since there is no
    passphrase yet to prove knowledge of. This is remotely resolvable by
    design — that's the whole point of a first-visit gate.
  - **Corrupt** (invalid JSON, not an object, or a store version this build
    doesn't understand): the auth gate activates exactly like an enabled
    login would, but with no way to ever pass it: every request is treated
    as unauthenticated (`GET /` shows the *login* page, not the setup page;
    APIs return 401), no existing session cookie can validate, a login
    attempt returns an explicit "Auth store is corrupted" error rather than
    "Incorrect passphrase", and — critically — `POST /api/dashboard-auth`
    is unreachable too, for every op. A corrupt security-relevant file must
    not be remotely overwritable, or a corrupted store would be a path to
    silently dropping the boundary it's supposed to enforce. Recovery is
    deliberately shell-only: repair or delete `dashboard_auth.json` on the
    host, which returns the dashboard to the missing-file state above (the
    setup gate, not the dashboard) — since the passphrase record is gone.
  - **Valid**: either login is enabled (gate active, cookie required) or the
    dashboard was explicitly opted out of login (`bootstrap_dismissed`),
    which is the one state that behaves like the dashboard always did —
    gate off, fully functional, "Set passphrase" available from Settings.
- **A fixed set of routes stay unauthenticated even with login enabled or
  pending**: `GET /api/version`, `/manifest.webmanifest`, `/sw.js`, and the
  PWA icons (needed for install and the version self-heal to work before
  login), plus `POST /api/login` itself (which, with no passphrase
  configured yet, answers "Login not configured" rather than checking one).
  Every other route requires a valid session cookie once login is on, or is
  blocked outright while the setup gate is pending.
- **Rate limiting**: a global (not per-IP — crr doesn't see the real client
  IP behind Tailscale Serve) in-memory failure counter imposes an
  exponentially growing server-side delay (`time.sleep`, capped at 300s)
  after 5 consecutive failures, on every subsequent attempt until one
  succeeds. The delay itself is the defense — there's no separate
  "unlock at time T" state to bypass. It resets on a successful login or a
  service restart.

## Defenses in depth (even inside the boundary)

- **Host-header allowlist** (DNS-rebinding defense): requests are accepted
  only when the `Host` header exactly matches loopback, this machine's
  hostname, a `*.ts.net` tailnet name, or a configured extra — port- and
  case-normalized. A rebound DNS name pointing at `127.0.0.1` is rejected.
- **JSON content-type required on POST** (`application/json`): this forces a
  CORS preflight for cross-origin callers, which neutralizes
  simple-request CSRF. There is **zero CORS** — no `Access-Control-*`
  response headers are ever emitted.
- **Strict input validation**: pids and session ids are validated against
  regexes before use. Actions are dispatched by an allowlist of operation
  names, never by reflecting client input into a command.
- **argv lists, never shell strings**: every external command crr runs is an
  argv list. No user- or journal-derived value is ever interpolated into a
  shell command line.
- **DOM via `textContent` only**: untrusted fields (cwd, last-prompt,
  session ids) are written to the page with `textContent`, never `innerHTML`,
  so a crafted transcript line cannot inject script.
- **`Cache-Control: no-store`** plus a `PAGE_VERSION` + `/api/version`
  self-heal: a client running a stale (possibly broken) page detects the
  version bump and reloads, so a bad cached page cannot brick the dashboard
  permanently.
- **Destructive operations are classifier-gated, never pid-only**: close /
  dismiss / reopen act on a session only after the classifier confirms its
  state, so a reboot-recycled pid can never cause an unrelated process to be
  signalled.

## What crr does *not* do

- It does not open any inbound port beyond the loopback dashboard.
- It does not phone home, and has **zero runtime dependencies** (stdlib-only
  server, dependency-free shell shims) — the supply-chain surface is the
  Python standard library.
- It does not read or transmit your conversation content anywhere; the
  last-prompt line is rendered locally into your own dashboard only.

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository
(**Security → Report a vulnerability**) rather than a public issue. Include
the version/commit, a reproduction, and the impact you observed.

Not affiliated with Anthropic.
