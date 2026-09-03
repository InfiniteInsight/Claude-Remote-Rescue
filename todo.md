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
      Slice 1 (core + CLI) implemented on feat/tunnel-provider (PR pending); slice 2 (dashboard GUI settings panel) remains.

- [x] Fix `crr deploy` from the PATH-linked binary + deploy-drift check in
      `crr doctor` — PR #110, merged 2026-08-28. Deploy resolved its repo from
      its own import location, so running the deployed copy always refused;
      that is why the snapshot sat 34 commits stale.
- [x] Windows Terminal launcher fallback — PR #111, merged 2026-08-29. A broken
      wt.exe App Execution Alias no longer stops tabs: open_tab falls through to
      the shell AUMID route (real WT tab, alias bypassed) then a plain console
      window, records which tier worked, and `crr doctor` reports it. The
      guidance-only spec (2026-08-27) is superseded — detection alone could not
      distinguish a disabled alias from a context where wt.exe cannot exec, and
      guidance never restores service.
      Manual Windows verification with the alias disabled: task #112.

## Blocked / manual

- [ ] Repair Windows Terminal App Execution Alias (manual, task #92)
- [ ] Windows node stopped serving after reboot: tailscaled holds a valid
      serve config but never bound its listeners (certs, firewall, ACL, and
      the WSL proxy target all verified healthy). Needs an elevated
      `Restart-Service Tailscale` on Windows. `hedylamarr-1.tail3af2d9.ts.net`
      works meanwhile.
