# Claude-Remote-Rescue

Keep your Claude Code sessions alive — and rescue them from your phone —
when terminals, shells, or whole machines die.

When a terminal window closes, a laptop reboots, or a VM runs out of
memory mid-conversation, Claude-Remote-Rescue notices, revives each
session (`claude --resume`) into tmux, and gives you a tailnet-only web
dashboard to reopen, dismiss, or remove any session from any device. It
also tells you *why* things died (journald — translated to plain English).

Not affiliated with Anthropic. MIT licensed.

Docs: [docs/site/](docs/site/) — servable via GitHub Pages.

## Status

**Phases 1–4 code is on `main` (v0.1.0).** Headless Linux (Phase 1) plus the
platform adapters — macOS (Phase 2: boot identity, launchd, Terminal.app /
iTerm2 tab-spawn, `log show`/`pmset` diagnostics), Linux desktop (Phase 3:
gnome-terminal / konsole / kitty / wezterm), and Windows/WSL (Phase 4:
`wt.exe` spawn, Scheduled Task, WinEvent + WSL-OOM diagnostics) — are built,
along with guessed/verified resume-sid tracking and the batched status probe.
See [DESIGN.md](DESIGN.md) and [ROADMAP.md](ROADMAP.md).

Honest calibration — what is and isn't verified:

- **Verified on Linux/WSL, in production.** crr is the author's daily-driver
  on a WSL2 machine: it owns the watchdog, the shell shim, and the tailnet
  dashboard, and tracks live sessions day to day. The **end-to-end hardware
  acceptance has been run** — a real reboot, after which the watchdog revived
  every conversation into tmux and the dashboard returned over the tailnet —
  and `kick`/`close`/`reopen`, the shim repair loop, and the recovery/recall/
  discovery/adopt(+`--takeover`) features are all live-exercised. The Linux CI
  matrix (Python 3.11/3.12), the `node --check` page-JS gate, and the
  import-linter layering contract pass; business logic is unit-tested with
  fakes.
- **NOT yet verified on real macOS or Windows.** Those platform adapters
  (launchd/`plutil`, `osacompile`/Terminal-tab spawn, `log show`/`pmset`, mac
  boot-identity; `wt.exe`, Scheduled Tasks, WinEvent/WSL-OOM) are unit-tested
  but have never run on the hardware. **macOS first-run testing is in
  progress** — see [docs/TESTING-macos.md](docs/TESTING-macos.md). Windows is
  next.

This is a ground-up OSS rewrite of a private Windows/WSL tool that
survived two real outages in production (a Windows-Update reboot and a WSL
OOM crash) with zero lost conversations; every decision marked **[lesson]**
in DESIGN.md was paid for the honest way.

## Dashboard

A tailnet-only, loopback-bound web dashboard for reopening, dismissing, or
removing any session from any device.

![The session dashboard](docs/screenshots/dashboard.png)

Each card carries a state badge (live / ghost / crashed), the `#pid · sid8`
identity, the sid's provenance (`injected` / `guessed` / `verified`), and the
last human prompt. Duplicate detection is **confidence-weighted** (audit P3):
two sessions that share an `injected`/`verified` id are a real **duplicate**
(blue); a collision that involves a `guessed` id is only a **possible
duplicate · guessed sid** (amber), because two `--continue` tabs can guess the
same newest transcript.

Cards sort by **true conversation recency** (the transcript's last turn, not
just the last shell action), mark the newest session per directory with a
**latest** chip, and carry a **compaction-pressure badge** that warns when a
transcript is close to the model's context window (so you know a revive will
compact). Every action button — `Kick`, `Close`, `Reopen`, `Untrack`, and the
rest — reports success or failure in a color-coded status toast. Two lazy
panels surface sessions crr *isn't* actively tracking: **Recently untracked**
(one-tap **Retrack**) and **Discoverable** — transcripts on disk crr never
journaled, each with **Adopt** and a confirm-gated **Take over** (stop a
still-live `claude` at a safe turn boundary, then adopt it — the phone-side of
`crr adopt --takeover`). A **search bar** (plus a per-card **Search**) recalls
earlier conversation from transcripts without re-injecting it into a live
session.

![Discoverable sessions, each with Adopt and Take over](docs/screenshots/discovery.png)

**Discovery** surfaces `claude` transcripts on disk that crr never tracked.
**Adopt** journals one as a recoverable session; **Take over** — the phone-side
of `crr adopt --takeover` — stops a still-live `claude` at a safe turn boundary
first, then adopts it, so no second copy races the first. A separate lazy
**"Why did sessions die?"** panel (not shown) leads with a plain-English
verdict — out-of-memory, kernel panic, unexpected shutdown, clean reboot, or
"looks clean" — above the raw journald/WinEvent evidence.

## Install

Requires Python ≥ 3.11 and `tmux`. Zero runtime dependencies otherwise.

### One-shot bootstrap (Linux, macOS, WSL)

The fastest path on a supported OS. It detects the platform, checks the
prerequisites (offering to install any that are missing — it never
force-installs a system package), installs crr with pipx, drops the shell shim
into the right rc file, installs the platform services, then sets up
Tailscale — the way you reach the dashboard from your phone: it offers to
install tailscale if it's missing, walks you through the one-time tailnet
sign-up (waiting for you to finish it in the browser), and offers to expose the
dashboard on your tailnet, explained in full and only after an explicit yes.
Idempotent: safe to re-run to refresh an install.

```sh
curl -fsSL https://raw.githubusercontent.com/InfiniteInsight/Claude-Remote-Rescue/main/bootstrap.sh | bash
# or, from a checkout (installs that checkout):
./bootstrap.sh
```

Flags: `-y` (unattended), `--dry-run` (show, don't change),
`--shell bash|zsh|fish`, `--tailscale` / `--no-tailscale`,
`--from-local` / `--from-git`.

### Manual install

> **Not on PyPI yet** (still pre-release). Install from source until the first
> published release:
> ```sh
> pipx install "git+https://github.com/InfiniteInsight/Claude-Remote-Rescue"
> # or, from a checkout:  pipx install .
> ```

1. **Shell shim** — source it from your rc file so shells journal
   themselves and `claude` launches become identifiable:
   ```sh
   crr shim fish >> ~/.config/fish/config.fish     # or: bash -> ~/.bashrc, zsh -> ~/.zshrc
   ```
2. **Watchdog + dashboard + keep-awake** — install the user services
   (autonomous revival, a dashboard that survives logout/reboot, and the
   keep-awake loop):
   ```sh
   crr systemd --install       # Linux; prints first with no args, so you can inspect
   crr launchd --install       # macOS (launchd user agents) — see docs/TESTING-macos.md
   ```
   Both install **three** units: revive, web, and `crr-awake`. The
   keep-awake loop does nothing until you turn it on — `power_block` is
   `off` by default (see [Keeping the machine awake](#keeping-the-machine-awake)).

   `crr schtasks` (Windows/WSL) installs the watchdog and dashboard
   **only** — no keep-awake task. Run `crr awake` yourself there, or
   install the systemd units inside WSL.
3. **Expose the dashboard on your tailnet** (loopback-only by default):
   ```sh
   tailscale serve --bg 8377
   ```
4. **Check the install:**
   ```sh
   crr doctor
   ```

macOS is unit-tested but not yet hardware-verified — if you're trying it there,
[docs/TESTING-macos.md](docs/TESTING-macos.md) is the first-run guide.

## Commands

| Command | What it does |
| --- | --- |
| `crr status [--json]` | List journaled sessions and their state (live / ghost / crashed) |
| `crr revive` | Revive crashed claude sessions into detached tmux (the watchdog runs this) |
| `crr reopen --pid N` (alias `crr restore --pid N`) | Revive one specific crashed or ghost session now (ghost: closes the orphaned shell and archives the conversation for revival) |
| `crr dismiss --pid N` | Clean up a crashed session without reviving (archives it) |
| `crr remove --pid N` | Delist a session, touch nothing else |
| `crr kick <pid>` | Restart claude in place on the same conversation |
| `crr close <pid>` | End a live session (remote exit); no revival |
| `crr untrack <pid>` (alias `crr detmux <pid>`) | Stop tracking a session — re-home a revived tmux session into a visible tab, archive it, and delist it (dashboard button: `Untrack` — the tab still runs tmux underneath) |
| `crr untmux <pid>` | Kill a parked tmux session and relaunch `claude --resume` directly in a visible tab, no tmux wrapper left behind (dashboard button: `Un-tmux`, confirm-gated) |
| `crr retrack [--last N \| --sid ID]` | Undo untrack/detmux: restore the N most-recently untracked sessions (default 10), or one by `--sid`, back into crr's management |
| `crr discover [--adopt SID]` | List untracked transcripts crr never journaled, or adopt one with `--adopt` into crr's management (revive via `crr reopen`; the watchdog may start a second `claude --resume` if the session is still alive elsewhere) |
| `crr adopt SID [--takeover] [--wait N]` | Adopt a discoverable session id. Plain, this is identical to `crr discover --adopt SID` — a session recorded as recoverable, not attached to any process. With `--takeover` (**destructive, default off**): first locate the live `claude --resume SID` process, wait for it to reach a clean turn boundary (an idle transcript ending in a finished assistant turn — never mid-tool-call or mid-reply), stop it in a way the shim's repair loop won't silently respawn, then adopt — so the watchdog's later revive is a genuine takeover: the sole `claude --resume SID` on that conversation, not a second one racing the first. Refuses (never kills) if: no live `--resume SID` process can be found (a freshly-started, non-`--resume` claude generates its sid internally and can't be located this way — start it with `--resume` first, or adopt without `--takeover`); it's still actively writing past `--wait` seconds (default: config `takeover_max_wait_seconds`, 180s); it goes quiet but is parked mid-turn or mid-reply, not a safe boundary; or the sid becomes tracked by something else in the small window before the kill |
| `crr rescued` | List conversations the reviver already parked in tmux from a previous boot, awaiting re-home |
| `crr diagnose [--json]` | Explain why the previous boot / sessions may have died |
| `crr gc` | Drop archive records past the retention window |
| `crr archive --list` | List archived (revival-preserved) sessions: reason, archived-at, sid8, cwd |
| `crr recall <query> [--pid N \| --sid ID \| --all] [--cwd DIR] [-n N]` | Search a session's transcript for earlier conversation (print-only, never re-injects) |
| `crr web [--port N]` | Serve the dashboard (loopback only) |
| `crr awake [--once]` | [service] Hold the machine awake while a Claude session is live. The loop, not a durable OS reservation: the hold is a child process of this command, so stopping it releases the hold. Off unless `power_block` is set. Lid close is never blocked |
| `crr power [--release]` | Report what crr is holding awake and why — or why not. `--release` stops the keep-awake loop (that *is* the release; there is no other handle) |
| `crr systemd [--install\|--uninstall]` | Print (or install/uninstall) the Linux user units: watchdog timer + dashboard + keep-awake (`crr-awake.service`) |
| `crr launchd [--install\|--uninstall]` | Print (or install/uninstall) the macOS launchd user agents (watchdog + dashboard + keep-awake) |
| `crr schtasks [--install\|--uninstall]` | Print (or install/uninstall) the Windows/WSL Scheduled Tasks (watchdog + dashboard). **No keep-awake task** — the command says so; run `crr awake` yourself or use `crr systemd --install` inside WSL |
| `crr config --effective` | Every config key with its value and origin (`configured` / `default`) |
| `crr doctor` | Install-health checklist |
| `crr shim <shell>` | Print the shell shim to source from your rc file (fish/bash/zsh) |
| `crr repair-check --pid N` | [shim] Read/clear a session's relaunch/close flag |
| `crr rescue-check` | [shim] Once per boot, offer to re-home rescued conversations into visible tabs |

Targets: headless Linux (now), macOS / Linux-desktop / Windows-WSL (later).
Shells: zsh, bash, fish.

## Keeping the machine awake

A remote Claude session dies when the machine it runs on sleeps. `crr awake`
is a loop that holds the machine up **only while a Claude session is
actually live**, and drops the hold the moment the last one ends.

**It is off by default.** A tool that silently stops your laptop from
sleeping is not a tool you asked for, so nothing happens until you set
`power_block` in `config.toml`:

| Key | Default | What it does |
| --- | --- | --- |
| `power_block` | `"off"` | `"off"`, `"sleep"`, or `"sleep+shutdown"`. What to hold while a session is live |
| `power_block_requires_ac` | `true` | Only hold while on AC. On battery — or when the power source **cannot be read** — crr holds nothing and says so |
| `power_block_max_hours` | `12` | Windows/WSL only: the holder child expires after this, so a crashed crr cannot pin the host awake forever |
| `power_poll_seconds` | `30` | How often the loop re-decides |
| `power_state_max_age_multiplier` | `3` | A report older than `power_poll_seconds ×` this is treated as UNKNOWN, not as still-true |

```sh
crr awake            # run the loop in the foreground (Ctrl-C releases)
crr awake --once     # one decide-and-apply pass, then release and exit
crr power            # what is held right now, and why — or why not
crr power --release  # stop the loop, which IS the release
```

**Closing the lid is never blocked**, on any platform. On Linux the hold is
`systemd-inhibit --what=sleep --mode=block`, and logind exempts the lid
from inhibitors by default; on a host that has turned that exemption off
(`LidSwitchIgnoreInhibited=no`) — or whose logind config crr cannot read —
crr **refuses the sleep hold and prints why** rather than touching the lid.

**The hold is a child process of `crr awake`, not a durable OS
reservation.** That is deliberate: it cannot outlive crr. It also means
there is no separate "unhold" button — stopping the loop is the release,
which is exactly what `crr power --release` does (`systemctl --user stop
crr-awake.service`, or the launchd equivalent on macOS). If you started
the loop by hand rather than through a unit — the headless escape hatch —
stop that process directly; `crr power --release` will say so if the unit
stop finds nothing.

**A hold is never invisible.** `crr awake` stamps a small `power.json` in
the state dir after every poll, and `crr power` / `crr doctor` read it
from their own separate processes. They distinguish four things that used
to look alike:

- `holding: sleep — <reason>` — a live, current, trusted claim.
- `holding: nothing — <reason>` — a **known** nothing (`power_block is
  off`, `no live claude session`, `on battery`).
- `holding: unknown — <reason>` — the report cannot be trusted right now:
  the loop that wrote it is gone, its last report is too old, the file is
  corrupt — or a hold was **asked for and not obtained**. `crr doctor`
  shows these as `[WARN]`, never `[ok  ]`.
- `NOT holding: sleep — asked for and not obtained` alongside a real
  hold — a **partial** hold; crr got some of what you asked for and names
  the rest.
