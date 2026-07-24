# Claude-Remote-Rescue

Keep your Claude Code sessions alive — and rescue them from your phone —
when terminals, shells, or whole machines die.

When a terminal window closes, a laptop reboots, or a VM runs out of
memory mid-conversation, Claude-Remote-Rescue notices, revives each
session (`claude --resume`) into tmux, and gives you a tailnet-only web
dashboard to reopen, dismiss, or remove any session from any device. It
also tells you *why* things died (journald — translated to plain English).

Not affiliated with Anthropic. MIT licensed.

## Status

**Phase 1 implemented — headless Linux.** The core, shell shims, reviver,
web dashboard, watchdog, and diagnostics are built and green in CI
(ubuntu + macOS runners, Python 3.11/3.12; the served page's JavaScript is
`node --check`-gated). See [DESIGN.md](DESIGN.md) and [ROADMAP.md](ROADMAP.md).

Honest calibration: everything below is covered by automated tests and
verified single-process on the author's box, **but the end-to-end
acceptance — kill the tmux server, reboot, watch every conversation
revive and the dashboard return over the tailnet — has not yet been run on
real hardware.** Two commands are deliberately not shipped yet: `kick` and
`close` (they signal live processes and need the shim repair loop; held
until revival is proven on hardware). macOS/Windows-desktop adapters are
Phases 2–4.

This is a ground-up OSS rewrite of a private Windows/WSL tool that
survived two real outages in production (a Windows-Update reboot and a WSL
OOM crash) with zero lost conversations; every decision marked **[lesson]**
in DESIGN.md was paid for the honest way.

## Install (headless Linux)

Requires Python ≥ 3.11 and `tmux`. Zero runtime dependencies otherwise.

```sh
pipx install claude-remote-rescue      # or: pip install --user .
```

1. **Shell shim** — source it from your rc file so shells journal
   themselves and `claude` launches become identifiable:
   ```sh
   crr shim fish >> ~/.config/fish/config.fish     # or: bash -> ~/.bashrc, zsh -> ~/.zshrc
   ```
2. **Watchdog + dashboard** — install the user services (autonomous revival
   + a dashboard that survives logout/reboot):
   ```sh
   crr systemd --install       # prints first with no args, so you can inspect
   ```
3. **Expose the dashboard on your tailnet** (loopback-only by default):
   ```sh
   tailscale serve --bg 8377
   ```
4. **Check the install:**
   ```sh
   crr doctor
   ```

## Commands

| Command | What it does |
| --- | --- |
| `crr status [--json]` | List journaled sessions and their state (live / ghost / crashed) |
| `crr revive` | Revive crashed claude sessions into detached tmux (the watchdog runs this) |
| `crr reopen --pid N` | Revive one specific crashed session now |
| `crr dismiss --pid N` | Clean up a crashed session without reviving (archives it) |
| `crr remove --pid N` | Delist a session, touch nothing else |
| `crr diagnose [--json]` | Explain why the previous boot / sessions may have died |
| `crr gc` | Drop archive records past the retention window |
| `crr web [--port N]` | Serve the dashboard (loopback only) |
| `crr systemd [--install]` | Print (or install) the watchdog timer + dashboard service |
| `crr config --effective` | Every config key with its value and origin (`configured` / `default`) |
| `crr doctor` | Install-health checklist |

Targets: headless Linux (now), macOS / Linux-desktop / Windows-WSL (later).
Shells: zsh, bash, fish.
