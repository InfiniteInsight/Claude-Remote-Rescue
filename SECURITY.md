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

## Dashboard login (optional)

crr can additionally gate the dashboard behind a passphrase — off by
default, since the tailnet is the primary boundary above. A bootstrap
prompt on first visit offers to set one up; it can be declined and
re-enabled later from Settings.

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
- **Fails closed on a corrupt auth store, open on a missing one**: these are
  different states. A *missing* `dashboard_auth.json` simply means login was
  never configured — the dashboard is reachable and the bootstrap prompt
  offers to set one up, exactly as on a fresh install. A *corrupt* one
  (invalid JSON, not an object, or a store version this build doesn't
  understand) activates the auth gate instead of disabling it: every
  request is treated as unauthenticated (`GET /` shows the login page, APIs
  return 401), no existing session cookie can validate, and a login attempt
  returns an explicit "Auth store is corrupted" error rather than
  "Incorrect passphrase". A corrupt security-relevant file must not be able
  to silently drop the security boundary it's supposed to enforce.
  Recovery is deliberately shell-only: repair or delete
  `dashboard_auth.json` on the host, then the dashboard is reachable again
  (with login disabled, since the passphrase record is gone).
- **A fixed set of routes stay unauthenticated even with login enabled**:
  `GET /api/version`, `/manifest.webmanifest`, `/sw.js`, and the PWA icons
  (needed for install and the version self-heal to work before login), plus
  `POST /api/login` itself. Every other route requires a valid session
  cookie once login is on.
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
