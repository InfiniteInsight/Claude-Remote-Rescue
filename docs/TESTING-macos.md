# Testing crr on macOS (first live run)

> **Status: crr's macOS code is unit-tested but has never run on a real Mac.**
> If you're reading this, you're the first — expect rough edges, and that's
> the point. This guide gets crr installed, tells you what to try, and — most
> importantly — what to watch for and how to report it back.

crr keeps Claude Code sessions alive and remotely rescuable: it tracks every
`claude` you launch, and a small background agent revives sessions whose
terminal/shell/host dies. A local web dashboard (127.0.0.1:8377) shows and
controls them.

## 0. Prerequisites

- **macOS** with **Python 3.11+** — check `python3 --version`. If older (or
  missing), `brew install python@3.12`.
- **pipx** — `brew install pipx && pipx ensurepath`, then open a new terminal.
- **tmux** — `brew install tmux` (crr revives sessions into tmux).
- Your shell is almost certainly **zsh** (the macOS default).

## 1. Install

Pick one:

```sh
# A) straight from GitHub (the repo is public — nothing to download):
pipx install "git+https://github.com/InfiniteInsight/Claude-Remote-Rescue"

# B) from the wheel file you were sent:
pipx install ./claude_remote_rescue-0.1.0-py3-none-any.whl
```

Verify:

```sh
crr --version
crr doctor        # install-health checklist — note anything it flags
```

## 2. Turn on session tracking (the zsh shim)

The shim is a dependency-free snippet that wraps `claude` so crr can track it:

```sh
crr shim zsh > ~/.crr-shim.zsh
echo 'source ~/.crr-shim.zsh' >> ~/.zshrc
```

**Open a new terminal** so it loads. From now on, every `claude` you run is
tracked.

## 3. Start the background agents (watchdog + dashboard)

```sh
crr launchd            # PRINT the two launchd agent plists first — read them
crr launchd --install  # write + load them into ~/Library/LaunchAgents
```

Open the dashboard:

```sh
open http://127.0.0.1:8377
```

Confirm the agents are loaded:

```sh
launchctl list | grep claude-remote-rescue
# expect: com.claude-remote-rescue.revive  and  .web
```

## 4. What to actually test

1. **Tracking** — run `claude` in a terminal, chat a little. A card for it
   should appear on the dashboard (refresh; it polls every few seconds).
2. **Crash + revival** — close that terminal window entirely. Within ~1 minute
   the watchdog should revive the conversation into a detached tmux session,
   and the dashboard card should flip to a recoverable/revived state. Then:
   ```sh
   tmux ls          # do you see a crr-<id> session?
   tmux attach -t crr-<id>   # is your conversation still there?
   ```
3. **⚠️ Revived-session survival (the #1 thing to confirm)** — after a revival,
   wait a couple of minutes (past when the 1-minute revive agent finishes) and
   check `tmux ls` again. **Does the revived session still exist?** On Linux it
   does; on macOS this is *unconfirmed* — launchd reaps process groups
   differently, and if the session vanishes when the agent exits, that's a real
   bug we need to fix (it needs `AbandonProcessGroup` in the plist).
4. **Dashboard buttons** — try Kick (restart in place), Close, Reopen, and the
   Search bar. Do they do what they say? Does the status toast report success?
5. **Auto tab-spawn** — some actions try to open a Terminal.app tab via
   AppleScript. Does a tab actually open, or does it error?
6. **Reboot** — restart the Mac, log back in, open a terminal. The dashboard
   agent should come back at login (it's a user agent, so it only runs while
   you're logged in — that's expected). Are prior sessions still recoverable?

## 5. How to report back

The launchd agents don't redirect their logs to files, so the most useful
signal is running the two commands **by hand** in a terminal and pasting what
they print:

```sh
crr doctor
crr web --port 8378          # run the dashboard in the foreground (diff port) to see its stderr; Ctrl-C to stop
crr revive                   # run one watchdog pass in the foreground; watch its output
log show --last 10m --predicate 'process == "crr"' --info   # unified-log fallback
launchctl print gui/$(id -u)/com.claude-remote-rescue.web    # agent status/last exit
```

Send: `crr doctor` output, anything from the above that errored, and a one-line
note on each of the six tests in §4 (worked / didn't / weird). Screenshots of
the dashboard are great too.

## 6. Remove it cleanly when done

```sh
crr launchd --uninstall
# then remove the shim: delete the 'source ~/.crr-shim.zsh' line from ~/.zshrc
rm -f ~/.crr-shim.zsh
pipx uninstall claude-remote-rescue
```

That leaves your Mac exactly as it was. Thanks for kicking the tires. 🙏
