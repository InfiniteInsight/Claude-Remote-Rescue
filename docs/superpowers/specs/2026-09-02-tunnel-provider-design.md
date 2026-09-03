# Pluggable tunnel support: Cloudflare Tunnel alongside Tailscale

**Date:** 2026-09-02
**Status:** approved (user-approved design; this doc is its record)
**Backlog:** todo.md "Pluggable tunnel support"

## Problem

The dashboard binds loopback:8377 and relies on Tailscale Serve to be
reachable from a phone — and that integration is read-only: CRR never
starts `tailscale serve`, it only reads status and prints the command to
run. There is no way to reach the dashboard without joining the tailnet,
which caps sharing at "devices Evan enrolls." Cloudflare's free tier
covers named Tunnels + Access (Zero Trust) for up to 50 users, but CRR
has no notion of a tunnel at all — no lifecycle, no health, no URL.

## Decisions (user, 2026-09-02)

- **Full lifecycle**: CRR starts/stops/health-checks the tunnel and
  advertises its URL — not detect-and-display.
- **Named Cloudflare tunnel only**: a stable hostname on the user's CF
  zone. Ephemeral trycloudflare quick tunnels are out (URL churn fights
  the QR/advertise model).
- **Cloudflare Access is out of scope**: the user configures Access in
  the CF dashboard; this spec documents the recommended setup. The
  app-level passphrase login (PR #107) already gates the dashboard.
- **One active provider, chosen by config key, editable from the GUI**:
  `tunnel_provider` default in config.toml, dashboard-writable override
  in the SettingsStore (the autokick layering pattern — no second source
  of truth).
- **Approach A**: the tunnel process is managed as a user-level service
  unit (systemd --user via the existing `crr/adapters/systemd.py`
  machinery), not supervised as a crr-web child. Units survive crr-web
  restarts and reboots; supervision stays where the platform already
  does it.

## Architecture

One-way layering as ever: `crr.cli` wires adapters into core; core sees
only the port.

### Port (`crr/core/ports.py`)

```python
class TunnelHealth(NamedTuple):
    state: str          # "up" | "down" | "unknown"  (F16 tri-state)
    detail: str         # human-readable ("unit inactive", "cloudflared not found")

class TunnelProvider(Protocol):
    def name(self) -> str: ...                    # "tailscale" | "cloudflare"
    def available(self) -> bool: ...              # binary present on this host
    def start(self, port: int) -> tuple[bool, str]: ...   # (ok, message)
    def stop(self) -> tuple[bool, str]: ...
    def health(self) -> TunnelHealth: ...
    def advertise_url(self) -> str | None: ...    # None = not derivable/up
    def setup_hint(self) -> str | None: ...       # what to run when prereqs missing
```

Tri-state discipline: `health()` returns `"unknown"` on any probe
failure and no caller treats unknown as down (no destructive action —
e.g. `reachable-at-boot` reports uncertainty, never "unreachable").

### Adapters

**`crr/adapters/cloudflared.py`** — named tunnel via a systemd user
unit:

- `start(port)`: refuse with the exact missing-prereq hint unless (a)
  `cloudflared` is on PATH, (b) `cloudflare_tunnel_name` and
  `cloudflare_hostname` are configured, (c) tunnel credentials exist
  (`~/.cloudflared/<uuid>.json` / origin cert — probed via
  `cloudflared tunnel info <name>` exit state). Then write
  `crr-tunnel.service` (ExecStart:
  `cloudflared tunnel --url http://127.0.0.1:<port> run <name>`) into
  the systemd user unit dir via the existing `systemd.write_units`
  pattern, `daemon-reload`, `enable --now`.
- `stop()`: `systemctl --user disable --now crr-tunnel.service`.
- `health()`: unit active state (`systemctl --user is-active`) as the
  primary signal; any query failure → unknown.
- `advertise_url()`: `https://<cloudflare_hostname>/` when configured,
  else None. Static derivation — the named-tunnel hostname is
  configuration, not discovery.
- One-time account setup stays manual and documented (see Runbook):
  `cloudflared tunnel login`, `cloudflared tunnel create <name>`,
  `cloudflared tunnel route dns <name> <hostname>`. CRR never drives CF
  auth.
- Platform scope: slice 1 implements Linux (systemd --user — the
  deployed host). On macOS/Windows `available()` is False with a
  setup_hint naming the gap; launchd/schtasks ports follow the existing
  per-OS adapter pattern as later work.

**`crr/adapters/tailscale.py`** — grows the same port surface over the
existing read-only class:

- `start(port)`: `tailscale serve --bg <port>` (daemon-persistent, no
  unit needed). `stop()`: turn off only the dashboard-port serve (exact
  flag pinned in implementation) — never `serve reset`, which would
  clobber unrelated serve config on the node.
- `health()`: serve status live → up; query failure → unknown.
- `advertise_url()`: existing `tailnet.self_dashboard_url` logic.
- `setup_hint()`: today's `crr qr` hint text moves here.

### Config (config-defaults v25)

- `tunnel_provider`: `"tailscale"` (default) | `"cloudflare"` | `"none"`.
  Default preserves current behavior exactly.
- `cloudflare_tunnel_name`: `""`.
- `cloudflare_hostname`: `""`.

### Settings overrides (GUI-writable)

`SettingsStore` gains `read_tunnel()` / `write_tunnel()` for
`{provider, cloudflare_tunnel_name, cloudflare_hostname}` (each nullable
= "no override"). Effective value = override ?? config default — the
`effective_global_autokick` pattern, same degraded-store fail-safe (a
corrupt store yields config defaults and is reported, never a guess).
Both the CLI and the dashboard read/write through this one path.

### CLI

`crr tunnel up | down | status`:

- `up`: resolve the effective provider; refuse with `setup_hint()` when
  prereqs are missing; start; print the advertised URL.
- `down`: stop the active provider.
- `status`: provider name, health state + detail, advertised URL — and
  for `none`, say so.

Integration points (all consult the effective provider instead of
hardcoding tailnet):

- `crr qr` renders the active provider's `advertise_url()`; its "run
  this" fallback becomes the provider's `setup_hint()`.
- The dashboard's QR/machines providers use the same URL source. The
  machines/launcher panel remains tailnet-only (it enumerates tagged
  tailnet peers; Cloudflare has no peer concept) — with provider
  cloudflare it shows only this machine's URL.
- `reachable-at-boot` reports the active provider's health; unknown is
  reported as unknown.
- Host allowlist: the effective `cloudflare_hostname` is added
  automatically alongside the `.ts.net` suffix — no manual
  `host_allowlist_extras` edit.

### Dashboard GUI (slice 2)

A Tunnel section in the existing Settings modal:

- Provider picker (default/tailscale/cloudflare/none — "default" clears
  the override so config.toml rules).
- The two Cloudflare fields (name, hostname).
- Read-only health + advertised URL line.
- Backed by `/api/settings`-style read/write endpoints reusing the
  settings provider/writer plumbing; PAGE_VERSION bump + hash pin +
  changelog entry per house rules. Writing settings does not
  start/stop anything — the user acts via `crr tunnel up` or an
  explicit Apply button that calls a tunnel action endpoint (POST,
  same action-provider pattern as session ops).

### Security posture (unchanged)

The web server stays loopback-only. The tunnel proxies in, exactly like
Tailscale Serve. App-level login (PR #107) remains the last line.
Runbook section documents: protect `<cloudflare_hostname>` with a CF
Access policy (free ≤ 50 users); CRR does not verify this.

## Error handling

- Every subprocess probe is tri-state; missing binary / timeout /
  nonzero / unparseable → None/unknown, never a raise (house adapter
  contract, mirrors `RealTailscale`).
- `up` on an unconfigured provider prints the setup hint and exits 2 —
  no partial unit writes.
- Switching providers while one is up: `up` for the new provider does
  not implicitly stop the old one; `crr tunnel status` names any other
  provider still healthy so nothing is torn down behind the user's
  back.

## Testing (TDD, red first)

- Port fakes in core tests: provider selection (override ?? config),
  effective-value degradation on corrupt store.
- Adapter tests with stubbed subprocess runners: unit file content pin,
  refuse-without-prereqs hints, tri-state health on every failure mode.
- CLI: `tunnel up/down/status` wiring pins (monkeypatched provider), qr
  URL source switch, allowlist gains the CF hostname, reachable-at-boot
  unknown-stays-unknown.
- Slice 2: PAGE_VERSION guard + settings endpoint round-trip tests.

## Slices

1. **Core + CLI** (one PR): port, both adapters, config v25, settings
   overrides, `crr tunnel`, qr/allowlist/reachable-at-boot integration,
   runbook doc. Useful standalone — GUI users wait one PR.
2. **Dashboard GUI** (one PR): Settings-modal Tunnel section + endpoints
   + Apply action.

## Runbook (shipped as docs, summarized)

One-time Cloudflare setup the user performs:
1. `cloudflared tunnel login`
2. `cloudflared tunnel create <name>`
3. `cloudflared tunnel route dns <name> <hostname>`
4. Recommended: CF Zero Trust → Access → protect `<hostname>`.
5. `crr tunnel up` (after setting provider + name + hostname via GUI or
   config.toml).
