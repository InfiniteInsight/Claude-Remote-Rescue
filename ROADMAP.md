# Roadmap

Each phase produces working, testable software and ends with real-world
verification on actual hardware before the next begins.

## Phase 0 — scaffold (this commit)

Founding docs. Then: Python package skeleton (`crr/`), pytest wiring,
MIT license, CI stub (ubuntu + macos), and the plumb-line-derived
guardrails from day one: ruleset file declaring the layer direction,
import-linter boundary check in CI, contract validators as the first
test fixtures (see DESIGN.md audit-requirement paragraphs).

## Phase 1 — core + headless Linux (testbed: the author's SSH server)

- Journal store, classifier (live/ghost/crashed), schema v1.
- zsh + bash + fish shims (register / last-cmd / deregister / claude
  wrapper with sid injection + repair loop). Absolute-path invocation.
- tmux reviver + give-up guard; sessions can be tmux-native (headless).
- `crr` CLI: status --json, kick, close, reopen, dismiss, remove,
  diagnose (journald sources), gc.
- Web dashboard port (all ccresume v18 features), systemd user units,
  watchdog timer, linger.
- Sid re-verification after picker-resume (fixes ccresume's known
  guessed-sid bug at the design level).

**Acceptance:** on a headless box reached only by SSH+tailnet: shells in
tmux journal themselves from all three shims; killing the host's tmux
server then rebooting brings every Claude conversation back revived;
dashboard reachable at `http://<host>/` via tailscale serve; all
session operations work from a phone; suite green on CI ubuntu.

## Phase 2 — macOS (testbed: the author's Mac laptop)

- Boot-identity adapter (`sysctl kern.boottime`), launchd agents,
  Terminal.app + iTerm2 spawn adapters (osascript), `log show`/`pmset`
  diagnostics adapter, state-dir path adapter.
- zsh shim polish (macOS default shell), Homebrew-friendly install.

**Acceptance:** close a Terminal.app window with a live Claude session →
ghost appears on dashboard → Reopen restores a new tab with the
conversation resumed; reboot the Mac → sessions revive at login; suite
green on CI macos.

## Phase 3 — Linux desktop terminals

- Spawn adapters: gnome-terminal, konsole, kitty, wezterm; detection +
  config override.
- **Built** (unit-tested; live-verified pending): restore-prompt UX —
  `crr rescue-check`, run once per boot from the shims, offers to
  re-home prior-boot conversations the reviver parked in tmux into
  visible tabs (`crr.core.rescue` + `crr rescued`/`crr rescue-check`).
  Headless hosts degrade to a notice; an unattended timeout always
  declines. Covered by CLI-level tests with every adapter (tmux, tab
  spawner, boot identity) faked — not yet exercised against a real
  post-reboot shell.

**Acceptance:** the Phase-2 window-close/reopen/reboot scenarios pass on
at least two of the four terminals on a real desktop.

## Phase 4 — Windows/WSL on the shared core

- Port the wt.exe spawn, Scheduled-Task watcher, PowerShell diagnostics,
  and WSL-boot semantics onto the Python core; migrate the author's
  production ccresume install; retire the fish/jq implementation after
  a parity soak.

**Acceptance:** the two historical outage scenarios (host reboot; WSL VM
OOM death) replay successfully on the new stack.

## Phase 5 — release polish

- Packaging: pipx / Homebrew formula / AUR; install doctor (`crr doctor`).
- **Built** (publish pending): the static, dependency-free docs site
  (`docs/site/` — hand-written HTML5 + CSS, zero external requests, dark-
  scheme aware, no JavaScript), covering the install flow, full command
  surface, dashboard screenshot, security model, and the honest
  calibration line. Not yet published to GitHub Pages. Screenshots,
  security-model writeup (SECURITY.md), and CONTRIBUTING remain.
- Public announcement.
