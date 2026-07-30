# Claude-Remote-Rescue — Design

Status: Phase 1 implemented for headless Linux (see README.md and
ROADMAP.md). This document remains the design of record; the maturity of
each piece — built/tested vs. hardware-verified vs. deferred — is
calibrated in the README's Status section, not here.

## Mission

Keep Claude Code sessions alive and remotely rescuable when terminals,
shells, or whole hosts die — on macOS, Linux (desktop and headless), and
Windows/WSL — controllable from any device on your tailnet.

A session that dies with your terminal should come back with its
conversation intact, either automatically (watchdog revival into tmux) or
with one tap on a phone dashboard.

## Provenance

This is a ground-up OSS rewrite of `ccresume`, a private Windows/WSL
implementation that ran production for its author through two real
outages (a Windows-Update reboot and a WSL VM OOM death) and recovered
every session both times. The design below encodes its battle-tested
lessons; the incidents that taught them are noted inline as **[lesson]**
markers because they are requirements, not trivia.

## Non-goals

- Not a terminal multiplexer (tmux is used, not replaced).
- Not a Claude Code fork: integration is via a shell wrapper function and
  reading transcript files; no private APIs.
- No cloud service, no accounts: the tailnet (or user-provided reverse
  proxy) is the entire access boundary.

## Architecture

One Python core (`crr` CLI + web server), thin per-shell hook shims, and
five narrow platform adapter interfaces. Python is chosen because the web
server already requires it, it removes ccresume's fish+jq dependency wall
(strangers won't have those), and it makes the core unit-testable with
pytest. Shims stay dependency-free by shelling out to `crr`.

**Requirement (audit P2 — One-way layering):** the layer direction below is
machine-enforced from day one, not prose: a committed ruleset file
(`CLAUDE.md`/`AGENTS.md`) declares it for agent sessions, and a CI check
(import-linter or equivalent) fails the build on an upward import. The one
named exception (composition root) is the CLI entry module, which wires
adapters to the core. ccresume's boundary held by review discipline alone —
the audit could only verify it by reading.

```
┌─ shell shims (zsh / bash / fish) ──────┐
│ register · last-cmd · deregister · claude() wrapper
└──────────────┬─────────────────────────┘
               ▼
┌─ crr core (Python) ────────────────────┐
│ journal store · classifier · reviver   │
│ kick/close/reopen/dismiss/remove/detmux│
│ diagnose · web server (stdlib http)    │
└──┬────────┬─────────┬────────┬─────────┘
   ▼        ▼         ▼        ▼
 boot     tab       service  diagnostics
 identity spawn     manager  source          ← platform adapters
```

### Journal store

One JSON file per session, `state_dir/tabs/<pid>.json`, schema v1:

```json
{
  "v": 1,
  "pid": 12345,
  "boot_id": "…",
  "cwd": "/home/u/project",
  "host": "tab | tmux | ssh",
  "shell": "zsh | bash | fish",
  "claude": {"session_id": "…", "sid_source": "injected | guessed | verified", "started": "…"} ,
  "last_cmd": "…",
  "tmux_session": null,
  "updated": "ISO-8601"
}
```

State dir: `$XDG_STATE_HOME/crr` (Linux), `~/Library/Application
Support/crr` (macOS). Writes are tmp-file+rename. Entries are created by
the shim at shell start, updated on cwd change / preexec, deleted by the
shim's exit hook.

**[lesson: PATH poisoning]** ccresume's registration silently died in
exactly the shells it existed to track because the hook ran before PATH
gained its dependencies. Shims must therefore invoke `crr` by absolute
path (resolved at install time), never by PATH lookup, and must be no-ops
(never error text into the user's prompt) when `crr` is missing.

**[lesson: env leakage]** Anything spawned from a revived tmux session
inherits the revival environment. Shims and the test-runner must scrub
`CRR_*` control variables rather than trusting a clean environment.

### Classifier

Three states, computed at read time:

- `live` — same boot identity AND pid alive AND shell owns a controlling
  terminal.
- `ghost` — same boot, pid alive, **no controlling terminal**. **[lesson:
  window-close orphans]** Closing a terminal window can kill the child
  process group but orphan the shell; without this state the dashboard
  shows healthy sessions that don't exist.
- `crashed` — pid dead OR boot identity mismatch.

Boot identity adapter: `/proc/sys/kernel/random/boot_id` (Linux),
`sysctl -n kern.boottime` (macOS). Terminal check is portable:
`ps -o tty= -p <pid>` (avoids /proc, works on macOS).

**[lesson: recycled pids]** Every destructive operation (close, dismiss,
reopen) gates on the classifier — never on bare pid-existence — or a
reboot-recycled pid gets an unrelated process killed.

### Claude wrapper (in each shim)

- Injects `--session-id <uuid>` on fresh launches so the session is
  identifiable; records it in the journal.
- Repair loop: on unexpected exit, offer (or on watchdog kick, silently
  perform) `claude --resume <sid>`.
- **[lesson: kill-by-ancestry]** Resumed sessions carry no `--session-id`
  on their argv. Any "kick" feature must kill by process ancestry (the
  claude child of the journaled shell, whole process group), never by
  cmdline pattern — pattern kills also risk unrelated processes.
- **[lesson: flag files]** Relaunch flags are written only when a kill
  actually lands, cleared at wrapper start; a stale flag silently resumes
  a session the user closed on purpose.
- **Requirement (audit P3 — Confidence + provenance):** the session id's
  origin travels with it. `sid_source` is `injected` (wrapper generated
  it — certain), `guessed` (derived from newest-transcript heuristics for
  picker/`--continue` resumes — uncertain), or `verified` (a guess later
  confirmed against the live transcript). Guessed sids are re-verified
  against their transcript on every status assembly and every revive
  sweep, and upgraded to `verified` once the transcript confirms them;
  `status --json` carries `sid_source` so the dashboard's duplicate
  detection can weight `guessed` claims accordingly instead of presenting
  them as truth.
  ccresume shipped without this and two tabs journaled the same sid in
  production (2026-07-21) — the laundering is observed, not theoretical.

### Reviver

`tmux` is the revival substrate on all platforms (a required dependency
for revival features; everything else degrades gracefully without it).
Crashed sessions with a Claude sid revive as detached tmux sessions
running `claude --resume`; tab adapters then attach visibly where tabs
exist. On headless hosts, tmux is not the fallback but the native home:
sessions may simply *start* under tmux and "revive" is just "reattach".

**[lesson: word-form exec]** Commands handed to tmux must be argv
word-form, or tmux wraps them in the login shell and journaling
double-registers.

**[lesson: give-up guard]** A revived session that dies again is archived,
not re-revived forever.

### Session operations (all classifier-gated, pid-keyed)

`kick` (restart claude in place, same conversation), `close` (remote
equivalent of typing exit), `reopen`/`restore` (single-session revival —
CRASHED spawns/notes-already-running as before; GHOST is the mobile
rescue path: close-flag the orphaned wrapper + kill claude's group(s) +
archive the entry with reason `ghost-restored` + delist + spawn into
detached tmux, kill-and-preserve strictly before spawn so a spawn failure
can never lose the conversation; LIVE refuses — kick/close are the ops for
a running claude), `dismiss` (clean up without restoring; archives crashed
entries), `remove` (pure delist, touches nothing), `detmux` (re-home a
revived tmux session into a visible tab; archives + delists on success —
the reviver owns `tmux_session`, so re-homing must leave its domain
entirely).
Semantics as proven in ccresume; failure statuses must propagate to the
web layer (**[lesson]** a
swallowed exit code turned hard failures into green checkmarks).

### Web dashboard

Port of ccresume's single-file stdlib server and page, including:

- Session cards: state badges (ghost/crashed/duplicate), identity
  tag `#pid · sid8`, duplicate group tinting, last-message line,
  contextual action buttons, indicator key, lazy diagnostics panel.
- **[lesson: page self-heal]** `PAGE_VERSION` + `/api/version` polling +
  `Cache-Control: no-store`: a cached page once shipped a JS syntax error
  and the self-heal is what un-bricks clients. Also: every `<script>`
  block is syntax-checked by tests (`node --check`) because a served
  page is not verifiable by curl.
- Last-prompt extractor skip-list (tool results, command wrappers,
  task-notifications, system-reminders, compaction continuations) — all
  discovered by real garbage on real cards.
- **Requirement (audit P7 — Contracted outputs):** the `/api/sessions` and
  `/api/diagnostics` payloads are versioned contracts, not incidental
  shapes: a version constant served alongside the data, a canonical key
  list, and a validator the tests import (the same validator the server
  can run in a debug mode). The journal file's `"v": 1` is the same rule
  applied to stored state. ccresume pinned these shapes only behaviorally
  in tests; a stored entry from an old version was indistinguishable from
  a current one.
- Security model (inherited wholesale): bind loopback only; tailnet (or
  user's own proxy) is the auth boundary; Host allowlist = {loopback,
  own hostname, *.ts.net, config extras} with exact-match semantics;
  JSON-Content-Type requirement on POSTs forces CORS preflight (kills
  simple-request CSRF); strict input validation (uuid/pid regex); argv
  lists only, never shell strings; DOM via textContent only.

### Diagnostics ("why did my session die")

Adapter interface: `boots()`, `prev_boot_errors()`, `host_events()`.

- Linux: journald (`--list-boots`, `-b -1 -p err`).
- macOS: `log show --last` filtered to shutdown/panic/watchdog events,
  `pmset -g log` for sleep/wake/thermal.
- Windows/WSL: Event Log Ids 1074/6008/41 via PowerShell (existing
  implementation), plus WSL-VM OOM forensics from the persisted journal.
  **[lesson: the 90GB that nobody owned]** OOM analysis must look at
  `inactive_anon`/shmem, not just process RSS — the killer's victims are
  usually bystanders.

Timeout-guarded, degrade per-source, fetched lazily by the UI (never on
the poll path).

### Service manager adapter

- Linux: systemd user units (service + watchdog timer), `loginctl
  enable-linger`.
- macOS: launchd user agents (`~/Library/LaunchAgents`).
- Windows: Scheduled Task + VBS-less hidden launch (existing).

**[lesson: interop PATH]** Service units get explicit, self-sufficient
PATH declarations; every external binary the service calls must resolve
in that PATH (a missing dir silently broke diagnostics until reviewed).

### Tab spawn adapter

Auto-detected, config-overridable:

- macOS: `osascript` for Terminal.app and iTerm2 (per-app scripts).
- Linux desktop: `gnome-terminal --`, `konsole -e`, `kitty`, `wezterm
  cli spawn` (each a ~5-line adapter; detection by `$TERM_PROGRAM`/which).
- Headless: none (attach instructions + tmux only).
- Windows: `wt.exe new-tab -p <profile>` (existing).

Spawn preflight: refuse to consume journal entries when the spawn binary
is absent (**[lesson]** delete-before-spawn without preflight destroys
the recovery state it exists to preserve).

## Config

One TOML file (`config.toml` in the state-dir parent): terminal choice,
wt/tab profile, archive retention, zombie action, host allowlist extras,
resume flags. Same shape as ccresume's, extended per-platform.

**Requirement (audit P5 — Injectable priors):** every constant that encodes
a judgment call is named config with a versioned default — never a magic
number in logic. The audit's caught set becomes the floor: zombie strike
count, close grace window, diagnose lookback window / event cap /
line cap / interop timeout, dashboard poll and version-check intervals,
last-prompt display cap. New timing or threshold decisions join this list
at introduction time, not at audit time. ccresume's watcher backoff/cooldown
and reopen tab-registration grace have no crr counterpart — the reviver's
strike-based give-up guard and the tmux-first reopen replaced those
mechanisms — so those knobs deliberately do not exist here (a knob wired to
nothing is worse than a magic number).

**Requirement (audit P3 — Confidence + provenance, applied to config):**
`crr config --effective` prints every key with its value AND its origin
(`configured` vs `default`), so a consumer can always distinguish an
explicit choice from an assumed one — defaults drive kill decisions, and
an invisible default is an invisible prior.

## Performance requirements

- Status assembly: ≤2 subprocess spawns per entry (**[lesson: snap jq]**
  a 90ms-per-launch binary times 7 calls times 24 cards = an unusable
  15-second dashboard). In Python this collapses to zero — the journal
  is read natively.
- Transcript scans stream backwards with early exit; poll endpoint must
  stay comfortably under the page's 5s poll cadence at 25+ sessions.

## Testing

- Core: pytest, no network, journal fixtures on tmpdirs.
- Shim contract tests per shell (zsh/bash/fish) using real shell
  subprocesses, the fake-tab pattern (`exec -a claude-fake sleep`), and
  `setsid`/pty tricks for ghost/tty states — all proven in ccresume.
- Web: stdlib-only HTTP tests + `node --check` page-JS gate.
- Platform adapter tests are gated by platform detection and skip
  cleanly elsewhere; CI matrix: ubuntu + macos runners (Windows adapter
  tested on real hardware initially).

## Licensing / naming

MIT. CLI binary: `crr`. Repo: `Claude-Remote-Rescue`. Not affiliated
with Anthropic; README must say so.
