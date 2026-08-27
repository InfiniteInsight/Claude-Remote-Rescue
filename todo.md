# TODO

## In progress

- [x] Merge `feat/persist-skip-permissions` branch — PR #105, merged 2026-08-25
- [x] Verify wt-window fix deployed correctly (confirmed 2026-08-25)

## Up next

- [x] Dashboard reauth — PR #106, merged 2026-08-26
- [ ] Optional dashboard login even on tailnet — app-level auth gate
      (token/passphrase) so CRR doesn't depend on the network layer for security
- [ ] Pluggable tunnel support — abstract tunnel lifecycle (start/stop/health-check/
      advertise-URL) so CRR can manage Cloudflare Tunnel the same way it manages
      Tailscale. Cloudflare free tier covers tunnels + Access (Zero Trust) for
      up to 50 users.

## Blocked / manual

- [ ] Repair Windows Terminal App Execution Alias (manual, task #92)
