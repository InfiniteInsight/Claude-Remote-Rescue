# CRR tray app: a Windows desktop home for Claude-Remote-Rescue

**Date:** 2026-11-05
**Status:** design, pending the owner's approval. Builder check done, and its questions are answered here.
**Issue:** #161

## Intent

crr runs headless inside WSL. Today its only faces are the dashboard in a browser, the phone, and the CLI. Nothing on the Windows desktop shows whether crr is running, starts or stops it, shows its logs, or edits its settings.

crr's services also can't reliably open Windows Terminal tabs. A process outside the user's interactive Windows session isn't allowed to create windows, so `tab_health` records launches as "launched, unconfirmed".

The owner built the Qwen Remote Launcher (`packages/launcher-app` in InfiniteInsight/qwen-code) to do this job for `qwen-rc`. They want the same for crr: everything that launcher does, plus every control the dashboard has.

## What the owner asked for

- A tray icon and a window, built with **Electron**, like the Qwen launcher.
- **Start and stop CRR.**
- **View logs.**
- **The QR code** for connecting the phone.
- **Every config setting.**
- **A Sessions tab** showing tracked sessions and their state (live, attached, restored, crashed). From it, you control sessions **the same way the web app does**.
- **Everything else the Qwen launcher does:**
  - a WSL distro picker
  - Start at login
  - opening terminal tabs on the service's behalf

## What it looks like

**Tray menu**, top to bottom:

- a status line (running or stopped, with session counts)
- Open CRR (the window)
- Open dashboard in browser
- Start/Stop CRR
- Start at login (a toggle)
- Quit tray app

**Tray icon:** three states.

- **Running:** CRR is up and nothing needs attention.
- **Needs attention:** any session is crashed, running elsewhere, or has two agents.
- **Stopped or unreachable.**

**Window tabs:**

- **Control.** One status row each for the watchdog, the dashboard and keep-awake, plus:
  - the WSL distro picker
  - the Start/Stop button
  - the QR code and the URL it encodes
  - whether a dashboard passphrase is set
- **Sessions.** The crr dashboard page itself (approach A below).
- **Logs.** A live tail of crr's service logs, with a picker for watchdog, dashboard or keep-awake.
- **Settings.** Every key from `crr config --effective`, grouped and editable. Each shows whether it's configured or the default, with a way to reset it to the default.

**Closing the window** hides it to the tray, as in the Qwen launcher. Only the tray menu quits the app.

The design conversation used two throwaway browser mockups: an overview, and a Sessions tab drawn as a native table. They aren't kept. Approach A replaces that table with the dashboard page, and its state filters and Claude Code version column move into the dashboard (see Changes to crr, item 6).

## Goals

1. Start, stop and see crr's state from the desktop without a terminal.
2. Every dashboard action, behaving exactly as on the web and the phone.
3. Logs and settings without editing files or remembering commands.
4. Get a phone connected with one scan.
5. Terminal tabs that crr opens reliably appear, and are confirmed.

## Non-goals

- **macOS or Linux builds.** Windows first. Keep code platform-neutral where it's cheap; don't build for other platforms yet.
- **An installer, code signing or auto-update.** v1 ships as an unpacked directory, as the Qwen launcher does today.
- **A second sessions UI.** Rejected as approach B.
- **Changing what the watchdog or dashboard decide.** The app is a front end.
- **New network exposure.** The app talks only to the loopback dashboard and to `wsl.exe`.

## Anti-goals

These would make the app a failure even if it worked:

- The app and the dashboard disagreeing about a session, or the app offering an action the dashboard would refuse.
- The app writing crr's state or config files itself, bypassing crr's validation and its mutation lock.
- **Stop CRR ending conversations.** Stop stops crr's services only. Sessions and their tmux panes keep running. This holds today: `crr-web` and `crr-revive` use `KillMode=process`, so stopping them leaves the tmux servers they spawned alone (#141).
- Secrets in the app's own storage, logs or command lines. That includes the dashboard passphrase and its signing secret.
- An Electron security regression:
  - Node integration in any renderer.
  - A preload exposed to the dashboard page.
  - Navigation that leaves the dashboard's origin inside the app.

## Constraints

- **crr's runtime dependencies stay at zero** (AGENTS.md). The app is a separate npm package in `tray/` with its own dependencies. The Python package never imports it, and `pyproject.toml` doesn't change.
- **The app runs on Windows, crr inside WSL.** The app reaches crr two ways only:
  - HTTP to the dashboard at `http://127.0.0.1:<dashboard_port>`. Verified reachable from Windows on the owner's host, which runs WSL in mirrored networking mode. WSL's default NAT mode forwards localhost too, but check that on a NAT host.
  - `wsl.exe -d <distro> -e <absolute crr path> <args>`, for everything that must work while the dashboard is stopped.
- **Don't rely on PATH inside `wsl.exe -e`.** It isn't a login shell, so it may not include `~/.local/bin`. Resolve crr's absolute path once, through a login shell or from the deployed copy at `$XDG_STATE_HOME/crr/app/bin/crr`, and store it in the app's settings.
- **The dashboard can require a passphrase.** The owner's does. The embedded page shows crr's own login page, and its session cookie lives in a persistent Electron session partition. The app never handles the passphrase itself.
- **crr's JSON outputs are contracts.** Every JSON shape the app reads from a crr command gets a version constant in `crr/core/contracts.py`, a validator, and a ledger entry, like every other contract (AGENTS.md).
- **New crr behaviour is test-first,** and import-linter layering holds: core stays pure, platform work goes in adapters, and the CLI wires them together.

## Approaches

The Sessions tab was the real choice. The rest of the plumbing was decided by the constraints above.

**A. Show the dashboard itself in the tab. Chosen.** The tab loads the existing dashboard page.

- Every button, confirmation and safety check is the dashboard's own, so the app and the web can't disagree.
- Anything added to the dashboard later appears in the app for free.
- The cost: the tab looks like the web page, and it's empty while CRR is stopped. It shows a "CRR is stopped, Start it" state instead.

**B. Rebuild the sessions view natively, against the dashboard's JSON API. Rejected.** It would look like the rest of the app, but it duplicates the dashboard's per-session logic:

- state badges
- which buttons appear when
- the confirmations
- Take over's conditions
- the two-agents warning

That leaves two copies to keep in step, on exactly the controls where a mismatch stops the wrong process or starts a second copy.

The owner chose A because it controls things "the same way the webapp does", literally.

Two other parts had only one sensible design:

- **Start/stop, logs, distro and settings go through `wsl.exe` running crr commands.** They must work while the dashboard is stopped. A new dashboard endpoint couldn't serve "start the dashboard".
- **Settings are written by crr, not the app, and never into `config.toml`.** `config.toml` stays hand-owned: crr already refuses to write it, because rewriting it would destroy the user's comments (`crr/core/exclusions.py`). The dashboard already follows this. Its changes go into crr-owned overlay files, `settings.json` and `exclusions.json`, layered over `config.toml`. The app's settings follow the same pattern (see Changes to crr, item 3). This is the project's existing rule, applied.

## Changes to crr (Python, this repo)

Each is its own issue, PR and contract.

1. **`crr service status|start|stop [--json]`.** Status reports each of crr's own units:
   - `crr-web.service` (dashboard)
   - `crr-revive.timer` (watchdog)
   - `crr-awake.service` (keep-awake)

   It also reports the dashboard URL(s) and whether the units are installed at all. Start and stop act on those three only.

   **What Stop means (owner's decision):** Stop lasts until you press Start, or until the next WSL restart or reboot, whichever comes first. A reboot always brings crr back.
   - So Stop is a runtime stop (`systemctl --user stop`), and the units stay enabled. It is never `disable`.
   - Status distinguishes "stopped, comes back at the next restart" from "not installed".
   - A `crr deploy` restarts `crr-web`, so it brings the dashboard back too, the same as a restart would.
   - While crr is stopped, the tray icon is grey and the status line says so.

   On macOS this maps to the launchd agents later. For now it fails clearly on platforms without an implementation.

   **v1 supports the systemd install only.** crr can also run on WSL as Windows Scheduled Tasks (`crr schtasks`), which has no keep-awake and no journald logs. On such a host, `service` and `logs` report "unsupported install" clearly instead of guessing.
2. **`crr logs [--service revive|web|awake] [--lines N] [--follow]`.** Wraps `journalctl --user -u <unit>` on Linux, plain text, one line per entry. With `--follow` it streams until killed.
3. **`crr config set KEY VALUE`, `crr config unset KEY` and `crr config --effective --json`.**
   - **Where `set` writes:** a crr-owned overlay, the same pattern as the dashboard's `settings.json`. Keys the dashboard already overlays (`tunnel_provider`, `cloudflare_tunnel_name`, `cloudflare_hostname`, `remote_control_autokick`, and the exclusions added to `discover_exclude_dirs`) keep using those existing stores, so the app and the dashboard edit one value, not two. `config.toml` is never written.
   - **`unset`:** removes the overlay value, so the key falls back to `config.toml` or the default.
   - **The JSON:** each key's effective value, type, and origin, which is one of `default`, `configured` (from `config.toml`) or `override` (an overlay). Today `crr config --effective` ignores the overlays, so it can report values that aren't in effect. This fixes that for the CLI too.
   - **Validation:** values are checked against `crr/core/config.py` DEFAULTS, and written atomically under the mutation lock.
   - **Applying a change:** each key carries one of three apply rules.
     - **Live:** read on use, so nothing else is needed.
     - **Reinstall:** baked into the installed units, so it needs `crr systemd --install`. That covers `dashboard_port` (in `crr-web`'s ExecStart), `watchdog_interval_seconds` (the timer) and `web_restart_seconds`. The app offers an Apply button that runs the reinstall and restarts `crr-web`. After a port change, it reloads the Sessions tab on the new port.
     - **Windows installer:** baked in by installers that need elevation or ask questions. That covers the `boot_*` holder keys and the `harden_*` keys. The app shows these read-only, with the command to run.
4. **`crr qr --json`.** Returns the URL the QR encodes (the tailnet dashboard URL), plus whether the dashboard is reachable. The app renders the QR itself, as the Qwen launcher does with its `qrcode` dependency.
5. **Tab handoff to the app.** This adds a tab-spawn tier `tray`, tried first on WSL while the app is alive.
   - **Request:** crr records the request (id, argv, cwd, title, created_at) in its state dir.
   - **Polling:** the app polls with `crr tray poll --json` every 2 seconds or so. Each poll records a heartbeat (app version, its own pid, time) and atomically claims pending requests.
   - **Result:** the app opens each tab with `wt.exe new-tab … wsl.exe -d <distro> --cd <cwd> -e <argv>`, then reports back with `crr tray done --id <id> --ok|--error <text>`.
   - **Liveness:** crr treats the tray as alive while the heartbeat is fresh, under a named, versioned config multiple of the poll interval. Otherwise the existing tiers (wt, aumid, console) run as today.
   - **Timeout:** a tray request that isn't claimed in time is withdrawn, and the next tier runs, so one tab never opens twice.
   - The request queue, heartbeat and result shapes are versioned contracts, and `tab_health` gains a `tray` tier.
6. **Dashboard additions.** These were agreed in the mockups, and with approach A they belong in the dashboard, so the phone gets them too.
   - **State filters, added to the existing "Show" dropdown** rather than replacing it, so its "Phone: not connected" option stays.
     - The new options cover every card state: live, attached (parked in tmux and someone has it open), restored (parked and nobody attached, the CLI's meaning), ghost and crashed.
     - Each option shows its count, so the counts add up to "All".
   - **Each card shows the Claude Code version that session runs,** read from Claude Code's per-process session file. That's a sessions contract bump and a PAGE_VERSION bump.

## The app (`tray/`)

- **Stack.** Electron, TypeScript, esbuild bundling, vitest for tests, and electron-builder for a Windows x64 unpacked build. That's the same toolchain as the Qwen launcher. Write fresh code informed by the launcher's structure rather than copying its files, so crr's MIT licence stays uncomplicated. (My call.)
- **Main process.** It owns:
  - the tray
  - the window
  - all `wsl.exe` calls
  - status polling
  - the tab-handoff loop
  - the app's own settings in Electron's `userData` JSON: distro, crr path, window bounds
- **The app's own renderer** handles the Control, Logs and Settings tabs. It runs with `contextIsolation`, `sandbox`, no Node integration, and a preload exposing a narrow `window.crr` API, one method per crr command above.
- **The Sessions tab** is a `WebContentsView` loading the dashboard URL.
  - It has no preload, uses a persistent session partition, and keeps context isolation and sandboxing.
  - It can't navigate away from the dashboard's origin, and outside links open in the default browser.
  - While CRR is stopped or unreachable, it shows a stopped state with a Start button.
- **Status.** The tray icon and status line need session state while the window is hidden, including at login before anyone has signed in to a passphrase-protected dashboard.
  - So the main process polls `crr status --json` over `wsl.exe`. It returns the same validated sessions payload the dashboard serves, with no login, and with `crr service status --json` it gives the whole picture.
  - It never adds an unauthenticated dashboard endpoint, and never borrows the Sessions tab's login cookie.
  - Polling is a few seconds apart, as named settings.
- **Never wake WSL.** Every `wsl.exe -d <distro>` call boots a stopped distro, and crr's holder exists so that a deliberate shutdown sticks (`crr holder pause`, `crr holder stop`).
  - So every poll first checks, without starting anything, whether the distro is running (`wsl.exe -l --running`) and whether a hold is in place.
  - If WSL is down, the app shows a "WSL is stopped" state and makes no `wsl.exe -d` calls.
  - Only an explicit action, such as Start CRR or Open dashboard, boots WSL.
- **Start at login** uses `app.setLoginItemSettings`.

## Testing

- **App.** Unit tests cover the pure logic:
  - parsing each crr JSON contract
  - the status reducer that drives the icon and menu
  - the tray menu spec
  - the handoff request handling
  - the log line framing

  They run in CI on Linux, as a new job. The Electron runtime pieces (windows, tray, `wsl.exe`, `wt.exe`, start at login) get an operator checklist run on Windows, like the Qwen launcher's `OPERATOR-VERIFY.md`.
- **crr.** Test-first as usual, with fakes for systemd and journald. Each new JSON shape gets its contract tests and ledger entry.

## Suggested order

1. crr commands: `service`, `config set/unset` with JSON output, `qr --json`, `logs`.
2. App skeleton: tray, window, Control tab, and the Sessions tab showing the dashboard.
3. Logs and Settings tabs.
4. Tab handoff: crr's `tray` tier and the app's launcher.
5. Dashboard additions: filter chips and the Claude Code version.

## Left to the builder

- Poll intervals and timeouts, as named config with versioned defaults where crr owns them.
- Icon art, window size and how settings are grouped.
- Whether Logs streams with `--follow` or polls.
- The exact `wt.exe` arguments and request file layout.
- How the app finds crr's absolute path the first time.
