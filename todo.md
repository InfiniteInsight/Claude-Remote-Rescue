# TODO

## In progress

- [x] Merge `feat/persist-skip-permissions` branch — PR #105, merged 2026-08-25
- [x] Verify wt-window fix deployed correctly (confirmed 2026-08-25)

## Up next

- [x] Dashboard reauth — PR #106, merged 2026-08-26
- [x] Dashboard login — PR #107, merged 2026-08-27. Default-on passphrase auth
      with explicit opt-out (blocking setup page on first visit), fail-closed on
      corrupt store, delay-then-verify rate limiting. App-level gate independent
      of the network layer.
- [ ] Pluggable tunnel support — abstract tunnel lifecycle (start/stop/health-check/
      advertise-URL) so CRR can manage Cloudflare Tunnel the same way it manages
      Tailscale. Cloudflare free tier covers tunnels + Access (Zero Trust) for
      up to 50 users.

- [x] Fix `crr deploy` from the PATH-linked binary + deploy-drift check in
      `crr doctor` — PR #110, merged 2026-08-28. Deploy resolved its repo from
      its own import location, so running the deployed copy always refused;
      that is why the snapshot sat 34 commits stale.
- [ ] `crr doctor` guidance for a broken wt.exe App Execution Alias — spec at
      docs/superpowers/specs/2026-08-27-doctor-wt-alias-guidance-design.md,
      branch feat/doctor-wt-alias-guidance (spec committed, not implemented)

## Blocked / manual

- [ ] Repair Windows Terminal App Execution Alias (manual, task #92)
- [ ] Windows node stopped serving after reboot: tailscaled holds a valid
      serve config but never bound its listeners (certs, firewall, ACL, and
      the WSL proxy target all verified healthy). Needs an elevated
      `Restart-Service Tailscale` on Windows. `hedylamarr-1.tail3af2d9.ts.net`
      works meanwhile.
