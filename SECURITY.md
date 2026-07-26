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
  (`tailscale serve`) or behind your own authenticating reverse proxy. There
  are no accounts, sessions, or passwords in crr itself — by design, there is
  no auth surface of our own to get wrong.
- If you expose the loopback port to a wider network yourself, you have
  removed the boundary crr relies on. Don't.

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
