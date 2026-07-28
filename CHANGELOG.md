# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] — unreleased

No tag or release has been cut yet. This section describes everything on
`main` as of the packaging work, pending the first public `v0.1.0` tag.

### Added

- Session journal + classifier: `register`/`deregister`/`last-cmd` shell
  hooks feed a per-shell journal, and a classifier turns raw journal state
  into `live` / `ghost` / `crashed` session status.
- Shell shims for fish, bash, and zsh (`crr shim <shell>`), each providing a
  `claude()` wrapper that injects and journals a `--session-id` on fresh
  launches and tracks resumed/continued sessions by guessed or verified sid.
- tmux reviver (`crr revive`) that resumes crashed sessions into detached
  tmux, with a give-up guard against repeated-crash loops and a
  cross-platform boot-identity adapter so revival only fires for sessions
  from the current boot.
- Remote session control: `crr kick` (restart claude in place on the same
  conversation) and `crr close` (cooperative remote exit, no revival),
  backed by a 3-state flag store (`relaunch` / `close` / absent) and a
  shim-side repair loop (`crr repair-check`) that each `claude()` wrapper
  consumes after every claude exit to branch on kick/close/crash.
- `crr detmux` — re-home a revived (tmux-parked) session into a visible
  terminal tab, with a matching dashboard button.
- Web dashboard (`crr web`), a stdlib-only HTTP server bound to loopback by
  default (meant to be exposed via `tailscale serve`), with sortable/
  groupable/filterable session cards, confidence-weighted duplicate
  detection, and versioned JSON contracts (`/api/sessions`, `/api/action`,
  `/api/diagnostics`) plus a versioned page bundle (`PAGE_VERSION`) so the
  server can detect a stale cached client.
- `crr diagnose` — a plain-English "why did sessions die?" verdict
  (out-of-memory, kernel panic, unexpected shutdown, clean reboot, or
  "looks clean") built from platform-native evidence: journald on Linux,
  `log show`/`pmset` on macOS, WinEvent + WSL-OOM detection on
  Windows/WSL.
- Cross-platform adapters: macOS (boot identity, launchd user agents,
  Terminal.app/iTerm2 tab-spawn), Linux desktop terminal spawn
  (gnome-terminal, konsole, kitty, wezterm), and Windows/WSL (`wt.exe`
  spawn, Scheduled Task, WinEvent + OOM diagnostics).
- Autonomous watchdog + service units: `crr systemd --install` (Linux
  timer + dashboard service), `crr launchd --install` (macOS user agents).
- `crr gc` (archive retention) and a TOML config loader
  (`crr config --effective`) reporting each key's value and whether it's
  configured or defaulted.
- `crr doctor` — an install-health checklist.
- `crr status [--json]` and last-prompt extraction so dashboard cards show
  the most recent human prompt per session.

### Changed

- **Any nonzero `claude` exit — including a SIGINT quit — now triggers the
  shim's resume offer.** Previously a nonzero exit returned silently to the
  shell prompt; now the `claude()` wrapper in each shim (fish, bash, zsh)
  prompts `Resume this conversation? [Y/n]`. Only an explicit `n`/`no`
  declines; anything else — including no answer because stdin isn't a
  terminal (bash/zsh time out via `read -t`; fish has no timed read) —
  resumes automatically, capped at 2 consecutive crash-resumes.

### Security

- The web dashboard binds to loopback only by default; remote access is
  opt-in via `tailscale serve`, not a bind-address change in `crr` itself.
- Host allowlist (exact match against loopback / own hostname / config
  extras, plus a `.ts.net` tailnet suffix) as a DNS-rebinding defense; no
  CORS headers are ever emitted and POST bodies are Content-Type-gated as
  CSRF defenses. The dashboard has no authentication of its own beyond
  this network-level model, inherited from the prior `ccresume` tool.
- Zero runtime dependencies: the web server is stdlib-only and the shell
  shims are dependency-free, by design (`pyproject.toml`
  `dependencies = []`) — a supply-chain guarantee, not an incidental state.

### Notes

- License: MIT.
