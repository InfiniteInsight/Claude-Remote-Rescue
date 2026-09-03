# Tunnels: reaching the dashboard from outside

The dashboard binds loopback only; a tunnel provider proxies it out.
Pick the provider in `config.toml` (`tunnel_provider = "tailscale" |
"cloudflare" | "none"`). A dashboard Settings override is planned
(slice 2); today config.toml is the configuration surface.

## Tailscale (default)

`crr tunnel up` runs `tailscale serve --bg <port>`. URL:
`https://<node>.<tailnet>.ts.net/`. `crr tunnel down` turns off only the
443 handler — it never runs `serve reset`.

## Cloudflare named tunnel

One-time setup (manual — crr never drives Cloudflare auth):

1. `cloudflared tunnel login`
2. `cloudflared tunnel create <name>`
3. `cloudflared tunnel route dns <name> <hostname>`
4. Set `cloudflare_tunnel_name` + `cloudflare_hostname` (+
   `tunnel_provider = "cloudflare"`) in config.toml.
5. `crr tunnel up` — installs and enables the `crr-tunnel.service`
   systemd --user unit running
   `cloudflared tunnel --url http://127.0.0.1:<port> run <name>`.

**Strongly recommended:** protect `<hostname>` with a Cloudflare Access
policy (Zero Trust → Access; free for up to 50 users). crr does not
verify this. The dashboard's own passphrase login remains the last line
either way.

## Notes

- `crr tunnel status` shows the active provider, tri-state health, and
  the advertised URL. Switching providers does not stop the old one —
  run `crr tunnel down` first if you want it gone.
- The Cloudflare hostname is admitted to the dashboard's Host allowlist
  automatically.
- Cloudflare lifecycle is Linux (systemd --user) for now; other hosts
  get an honest "not supported on this host yet" instead of a raw
  systemctl error.
