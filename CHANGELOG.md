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
  since it kills and relaunches. (PAGE_VERSION 12; confirm-gate state
  hardened at 13)
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
  boot. A typed empty line (Enter) defaults to yes; an unattended
  timeout always defaults to "not now" — it never auto-spawns tabs.
  Headless hosts (no tab spawner) degrade to a one-line notice instead
  of a prompt. New config key
  `rescue_prompt_timeout_seconds` (default 15).
- Docs site (`docs/site/`): a static, dependency-free HTML5 + CSS site —
  no JavaScript, no CDN/webfonts/analytics, dark-scheme aware via
  `prefers-color-scheme`, works from `file://` and GitHub Pages —
  covering the mission, how-it-works pipeline, the restore-prompt UX, the
  install flow, the user-facing `crr` command table, dashboard screenshot and
  button set, the security model, and the project's honest calibration
  line. Linked from the README; not yet published to GitHub Pages.
- `crr archive --list` — the human read path archive lineage lacked: one
  line per archived record (`reason`, `archived_at`, `sid8`, `cwd`),
  newest first, read-only.
- `/api/diagnostics` (contract v3) now carries `params` — the generating
  caps/lookback/timeout the selected source actually queried with, per
  source (journald/log+pmset/winevent+wsl-oom) — so a payload's evidence
  is regenerable/judgeable later instead of losing that lineage the
  moment it was collected. Both `crr diagnose` (human) and the dashboard's
  diagnostics panel now show the payload's `source`/boot-identity
  provenance up front, before the verdict.
- New config keys: `dashboard_port` (8377, replacing four repeated
  `--port 8377` argparse defaults), `web_restart_seconds` (2, the
  systemd dashboard service's `RestartSec`), `confirm_arm_seconds` (4),
  `notice_seconds` (3), `reload_delay_ms` (800), and
  `diag_error_display_cap` (20) — the dashboard's confirm-button arm
  window, notice-banner duration, stale-page reload delay, and
  diagnostics error display cap, previously hardcoded in `page.html`
  (`PAGE_VERSION` 13 → 14) — plus `model_tail_lines` (200), the
  transcript model-search tail window.

### Changed

- **Any nonzero `claude` exit — including a SIGINT quit — now triggers the
  shim's resume offer.** Without the crr shim, a nonzero `claude` exit returns silently to the shell prompt; with it, the `claude()` wrapper prompts to resume. Only an explicit `n`/`no`
  declines; anything else — including no answer at all — a non-tty stdin skips the prompt entirely in all three shells, and bash/zsh additionally time out an unanswered prompt after 30 seconds via `read -t` (fish has no timed read) —
  resumes automatically, capped at 2 consecutive crash-resumes.

### Fixed

- The distro name and `wt.exe` path were frozen into `crr-web.service` at
  install time (a service inherits neither `WSL_DISTRO_NAME` nor the Windows
  directories on `PATH`), so renaming the distro or moving the Windows user
  profile broke tab spawning until someone re-ran `crr systemd --install`.
  Both are now resolved at call time: `host.distro_name()` parses
  `wslpath -w /` (a Linux-side binary — no interop needed, and it reports the
  *current* registered name), and `tab_spawn_windows.wt_path()` falls back to
  a `/mnt/*/Users/*/…/WindowsApps/wt.exe` search when `PATH` comes up empty.
  The baked values remain as fallbacks, so a host without `wslpath` behaves
  exactly as before.

- Tab spawning borrowed `interop_timeout_seconds` (5s) — a budget shared with
  `ps`/tmux probes, where short is correct. A cold Windows Terminal launch can
  exceed it and still open the tab, so crr reported a false `NO TAB` while a
  tab appeared anyway. Tab spawning now has its own
  `tab_spawn_timeout_seconds` (default 30), which costs nothing when the
  terminal is warm. A timeout is also no longer reported as a failure: it
  raises `ports.TabSpawnTimeout` and is worded *no tab confirmed within Ns —
  the terminal may still be starting*, because "the command did not finish in
  time" is not "no tab appeared". Automatic retry was considered and rejected
  — a tab spawn is not idempotent, so retrying a slow-but-successful spawn
  opens a second tab.

- The dashboard showed nothing at all while an action was in flight, so a
  slow reopen looked ignored. Actions now raise a sticky *working…* notice for
  the whole round trip, and a degraded result carries a manual **Retry**
  control — manual because only the user can see whether a tab actually
  appeared.

- tmux sessions were named `crr-<first 8 chars of the session id>`, and crr
  uses that name as the identity of a parked conversation — so two
  conversations sharing those 8 characters collided, and Reopen silently
  attached the user to the *wrong* conversation while reporting success.
  `sid8` is a display abbreviation (payload contract, dashboard cards); using
  it as a key was the defect, not its width. Sessions are now named by the
  **full** session id, which removes the collision by construction rather
  than lowering its odds. `resolved_session_name()` prefers the name already
  recorded in `entry["tmux_session"]`, so conversations already parked under
  a legacy `crr-<sid8>` keep answering to it — without that, `reviver._decide`
  and `ops.reopen` would both read the unmatched name as "not running" and
  spawn a second `claude --resume` on a conversation that already has one.
  tmux resolves `-t` by prefix, so `tmux attach -t crr-79e5` still works.

- Reopen delivered a session but not always the tab, and reported that as
  plain success. The tab is part of what Reopen means, so a revival with no
  tab on a tab-capable host is now a distinct **degraded** outcome: `OpResult`
  carries `degraded`, `/api/action` returns it (still HTTP 200 — the session
  is alive), the dashboard shows an amber `NO TAB — …` notice instead of a
  green one, and `crr reopen` prints a warning to stderr while keeping exit 0
  so scripted callers do not start treating a live session as a failure.
  Hosts that have no tabs at all (headless, SSH, a systemd timer) are not
  flagged — `_tab_spawner` now reports `(spawner, tabs_expected)` so core can
  tell "never possible here" from "should have happened and didn't".

- The dashboard resolved its tab spawner once, at service startup. On WSL
  that decision depends on the `WSLInterop` binfmt handler, which can be
  absent at boot and repaired minutes later — so a long-lived `crr web` that
  started while interop was down opened no tab for the rest of its life, with
  no way to tell from the UI. The spawner is now resolved per action.

- Reopen on WSL reported only `(tab spawn failed: [Errno 8] Exec format
  error: 'wt.exe')` when the `WSLInterop` binfmt handler was missing — a
  systemd remount of `/proc/sys/fs/binfmt_misc` replaces the filesystem
  instance WSL registered into at boot, leaving `wt.exe` on `PATH` but
  unexecutable (live incident, 2026-08-09). Two fixes: the Windows Terminal
  spawner now reports itself unavailable unless an interop handler is
  actually registered (`shutil.which` cannot detect this — DrvFs marks every
  file executable), so reopen degrades to the honest `attach with: tmux
  attach -t …`; and the tab-spawn failure path now carries that same manual
  fallback instead of a bare errno. The revival itself was durable and the
  session attachable throughout; only the convenience tab was lost.

- The dashboard service re-read `page.html` from disk on every request, so a
  branch checkout under a running service could serve a template whose
  placeholders the loaded code cannot substitute — one raw `@PLACEHOLDER@` is
  a JS syntax error and the page renders nothing (live incident, 2026-08-01).
  The template is now snapshotted once at service startup; a restart is the
  deliberate deploy step.

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
- `crr rescue-check`'s once-per-boot prompt was check-then-act
  (`already_prompted`/`mark_prompted`): two interactive shells starting
  together (e.g. a terminal app restoring several tabs) could both pass
  the exists() check before either wrote the marker, so both could
  prompt and both detmux the same rescued sessions. Closed via an atomic
  marker claim (`rescue.claim_prompt`, an `os.open(O_CREAT|O_EXCL)`
  claim taken before either visible outcome) so at most one shell ever
  prompts even when several start at once.
- `RealTmux.list_sessions()` collapsed a query failure (timeout, OSError,
  an unrecognized nonzero exit) into the same empty set as "genuinely no
  sessions" — a transient tmux failure could accumulate a revive strike,
  or refuse an op with the wrong reason, against a session that might
  still be alive. It now returns `None` for an unknown state (distinct
  from a confident empty set when tmux itself reports no server); the
  reviver skips its entire pass on `None` rather than acting on a guess,
  `crr reopen`/`detmux`/`untmux` refuse with "cannot determine tmux state
  — is tmux responding?", and `crr rescued`/`rescue-check` degrade to
  silent/empty rather than ever prompting on an unconfirmed state.
- `crr doctor` printed only 3 of its 6 declared contract versions
  (omitting archive/config-defaults/page); `crr config --effective`
  never printed which `CONFIG_DEFAULTS_VERSION` generation it was
  reporting. Both now print the full, honest set.
- `crr status` (human) collapsed a `guessed` duplicate into the same
  `[dup]` tag as a certain (verified/injected) one, and never showed
  `sid_source` at all outside a duplicate group — the dashboard already
  renders this distinction; the CLI now does too (`[dup? guessed]` vs
  `[dup]`, and a ` sid:guessed`/` sid:verified` suffix whenever the sid
  isn't the certain `injected` norm).
- `crr revive`/`crr gc` reported gave-up/removed sessions as bare counts;
  they now name the pids/sid8s, matching the naming discipline every
  sibling problem-loop already uses.

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

