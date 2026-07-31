# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] — unreleased

No tag or release has been cut yet. This section describes everything on
`main` as of the packaging work, pending the first public `v0.1.0` tag. Licensed MIT.

### Added

- Session journal + classifier: `register`/`deregister`/`last-cmd` shell
  hooks feed a per-shell journal, and a classifier turns raw journal state
  into `live` / `ghost` / `crashed` session status.
- Shell shims for fish, bash, and zsh (`crr shim <shell>`), each providing a
  `claude()` wrapper that injects and journals a `--session-id` on fresh
  launches and tracks resumed/continued sessions by guessed or verified sid.
- tmux reviver (`crr revive`) that resumes crashed sessions into detached
  tmux, with a give-up guard against repeated-crash loops and a
  cross-platform boot-identity adapter so a session from a previous boot is recognized as crashed rather than mistaken for live under pid reuse.
- Remote session control: `crr kick` (restart claude in place on the same
  conversation) and `crr close` (cooperative remote exit, no revival),
  backed by a 3-state flag store (`relaunch` / `close` / absent) and a
  shim-side repair loop (`crr repair-check`) that each `claude()` wrapper
  consumes after every claude exit to branch on kick/close/crash.
- `crr detmux` — re-home a revived (tmux-parked) session into a visible
  terminal tab, with a matching dashboard button (labeled `Untrack` on the
  dashboard — the tab still runs tmux underneath, so the button no longer
  claims otherwise; the op/API name stays `detmux`).
- `crr untmux` — the genuinely tmux-free counterpart: kills the parked
  tmux session and relaunches `claude --resume <sid>` directly in a
  visible tab, no wrapper left behind. Same classifier/parked/live gates
  as `detmux`, plus a spawner-availability refusal that runs *before* the
  kill so a missing spawner never destroys a live tmux session. Archives
  successes with reason `untmuxed` (terminal — not revived by the
  watchdog). Dashboard button: `Un-tmux`, confirm-gated (a second click)
  since it kills and relaunches. (PAGE_VERSION 12)
- Web dashboard (`crr web`), a stdlib-only HTTP server bound to loopback by
  default (meant to be exposed via `tailscale serve`), with sortable/
  groupable/filterable session cards, confidence-weighted duplicate
  detection, and versioned JSON contracts (`/api/sessions`, `/api/diagnostics`) plus a versioned page bundle (`PAGE_VERSION`) so the
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
- Dashboard poll/version-check cadence (`dashboard_poll_seconds`,
  `version_check_seconds`) is now sourced from `crr.core.config` instead of
  being hardcoded in `page.html` (`PAGE_VERSION` 9 → 10).
- `crr reopen` gained a `restore` alias (`crr restore --pid N`), matching
  DESIGN's "reopen/restore" naming for the op.
- `crr reopen`/`restore` now rescues GHOST sessions too, not just CRASHED
  ones — the mobile Restore path a phone-only dashboard user otherwise
  lacked (Close on a ghost destroyed revival data). It close-flags the
  orphaned wrapper, kills claude's process group(s), archives the entry
  with a new reason (`ghost-restored`) before ever attempting a spawn, and
  revives it into detached tmux; a dashboard ghost card now shows a
  "Restore" button alongside Kick/Close (`PAGE_VERSION` 10 → 11).
- `crr systemd`, `crr launchd`, and `crr schtasks` gained `--uninstall`
  (mutually exclusive with `--install`) to reverse the watchdog/dashboard
  service installation.
- `crr.core.ports.DiagnosticsSource` — the de-facto `SOURCE_NAME`/
  `available`/`collect` contract already shared by the three diagnostics
  adapters (journald/macOS/Windows) is now a declared core port.
- `crr status` and the web dashboard's session provider re-verify guessed
  session ids at status-assembly time, not only on the watchdog's revive
  sweep, taking the mutation lock only when an upgrade is actually
  available to write (poll path stays lock-free otherwise).
- `CONFIG_DEFAULTS_VERSION` bumped to 2: dropped the consumer-less
  `watcher_backoff_count`, `watcher_cooldown_seconds`, and
  `reopen_grace_seconds` keys (no crr mechanism reads them).
- Restore-prompt UX (Phase 3): `crr rescued` lists prior-boot
  conversations the reviver already parked in live tmux, awaiting
  re-homing; `crr rescue-check` — a shim-facing (`[shim]`) hook called
  once per interactive shell start (the bash/zsh/fish shims all call it)
  — offers to re-home that same set into visible terminal tabs, once per
  boot, via an atomic marker claim so at most one shell ever prompts even
  when several start at once. A typed empty line (Enter) defaults to
  yes; an unattended timeout always defaults to "not now" — it never
  auto-spawns tabs. Headless hosts (no tab spawner) degrade to a one-line
  notice instead of a prompt. New config key
  `rescue_prompt_timeout_seconds` (default 15).

### Changed

- **Any nonzero `claude` exit — including a SIGINT quit — now triggers the
  shim's resume offer.** Without the crr shim, a nonzero `claude` exit returns silently to the shell prompt; with it, the `claude()` wrapper prompts to resume. Only an explicit `n`/`no`
  declines; anything else — including no answer at all — a non-tty stdin skips the prompt entirely in all three shells, and bash/zsh additionally time out an unanswered prompt after 30 seconds via `read -t` (fish has no timed read) —
  resumes automatically, capped at 2 consecutive crash-resumes.

### Fixed

- The watchdog's revive sweep no longer resurrects a `dismiss`ed session:
  the terminal-reasons skip set now includes `dismissed` alongside
  `gave-up`/`detmuxed` (the two `superseded-*` reasons stay revivable).
- `crr systemd|launchd|schtasks --install` now propagates installer
  command failures instead of unconditionally printing success and
  exiting 0; `schtasks --install` also refuses to run when
  `schtasks.exe` isn't on PATH rather than silently no-op'ing.
- `crr kick`/`crr close` now target only the shell's claude-prefixed
  child process groups (ancestry + argv0-basename match), so an
  unrelated background job (e.g. `make &`) is no longer swept into a
  remote Kick/Close signal; the relaunch/close flag is now cleared only
  when zero group kills land, so a partial kill no longer masks a
  still-live claude behind a false "handled" flag.
- `crr detmux` (and its dashboard button) are now classifier-gated to
  CRASHED sessions like every other destructive op — a live/ghost card
  can carry a stale `tmux_session` field, and previously the op had no
  gate at all. (PAGE_VERSION 9)
- `crr systemd` now bakes `wt.exe`/`wsl.exe`'s resolved dirs into the
  service PATH on WSL: neither lives in `SERVICE_BINARIES` + system dirs,
  so the deployed `crr-web.service` couldn't resolve them and the
  dashboard's tab spawner reported "no terminal tab spawner is available
  on this host" for both De-tmux and Reopen even though an interactive
  shell (which inherits the Windows-appended PATH) resolved both fine.
  `resolve_service_path` takes an `extra_binaries` param for this so
  `SERVICE_BINARIES` — and non-WSL Linux's PATH/warnings — stay
  unchanged. The same service-doesn't-inherit-the-shell gap also applied
  to `WSL_DISTRO_NAME` (read at request time to target `wsl.exe
  --distribution <name>`), so it's now baked into the unit the same way
  `XDG_STATE_HOME` already was — a multi-distro host would otherwise
  silently open the tab in the default distro instead of this one. The
  "not found on PATH" warning also no longer claims wt.exe/wsl.exe going
  missing will make "revived sessions fail on exec" — that's true only
  for the original `SERVICE_BINARIES`; a missing tab-spawn extra gets its
  own, accurate warning. Reopen's tab-open fallback also no longer
  returns a silent `""` when no spawner is available: it now names the
  reason and gives the `tmux attach -t <name>` command, so a revival that
  landed but couldn't open a visible tab doesn't look like it did
  nothing.
- `crr systemd --install` no longer fails the whole install when only
  `loginctl enable-linger` exits nonzero (live evidence, WSL2: this
  reliably fails with a benign dbus quirk while the timer/web services
  come up fine, since the user manager starts with the session anyway) —
  the exit-code-honesty fix above was over-claiming failure in the other
  direction. `daemon-reload`/`enable --now` failures still hard-fail the
  install (exit 1, no success line); a linger-only failure now exits 0,
  prints the success line, and warns on stderr instead. The split lives
  in the adapter (`systemd.critical_enable_commands()` +
  `systemd.linger_command()`); `systemd.enable_commands()` is unchanged
  for `crr systemd` print mode.

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
- Session ids are pinned to the UUID shape claude always issues them in
  (`contracts.valid_session_id`), enforced at the journal/session-card
  contract, `ArchiveStore.path_for`, `derive_resume_sid`, and
  `claude-launch`'s explicit `--session-id`. Closes a path-traversal
  (`ArchiveStore.path_for` building `f"{session_id}.json"` from an
  unvalidated sid) and a transcript-glob-injection hole reachable via a
  user-typed `claude -r '<sid>'`.

