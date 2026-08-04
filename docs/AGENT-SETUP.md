# Set up Claude-Remote-Rescue (crr) — a walkthrough for Claude Code

> **You are a coding agent (Claude Code) running on the user's own machine.**
> Your job: install and configure **crr** as far as you safely can on your
> own, then walk the user through the few steps that need them. Follow this
> file top to bottom. crr keeps Claude Code sessions alive and remotely
> rescuable: it tracks every `claude` you launch and a small background agent
> revives sessions whose terminal, shell, or host dies, with a local web
> dashboard to control them.
>
> **Be honest about newness.** crr's macOS support is unit-tested but has
> **never run on real Apple hardware** — this may be the very first time. Expect
> rough edges. When a step fails, **capture the exact error verbatim and keep
> going where safe** — do not silently work around it. These failures are the
> entire point of this run; the user will forward them to crr's author.

## Ground rules for you, the agent

1. **Do the safe, reversible steps yourself** without asking: environment
   checks, `pipx install`, `crr --version`, `crr doctor`, generating the shim
   text, and **printing** (not installing) the background-service definitions.
2. **STOP and get explicit confirmation before anything that changes the
   user's system**, namely: editing their shell rc file, installing the
   background service (`crr launchd --install` / `crr systemd --install`), or
   `brew install`-ing packages. Show the exact command and one line of *why*,
   then wait for a yes.
3. **Verify each step** — run the check command and show its output — before
   moving to the next.
4. **Touch only crr's own setup.** Never modify unrelated files or services.
5. At the end, **produce a short report** the user can copy-paste back to crr's
   author.

## Step 0 — Detect the environment (safe; do it, then report)

```sh
uname -s            # Darwin = macOS, Linux = Linux
echo "$SHELL"       # /bin/zsh (macOS default), /bin/bash, or a fish path
python3 --version   # need >= 3.11
command -v tmux pipx claude
```

Tell the user plainly if any prerequisite is missing:
- **Python < 3.11 or missing:** macOS `brew install python@3.12`; Linux use the
  distro package manager.
- **tmux missing:** `brew install tmux` (macOS) / distro package (Linux). crr
  revives sessions into tmux, so this is required.
- **pipx missing:** `brew install pipx && pipx ensurepath`, then a new shell.
- **claude missing:** they need Claude Code installed for crr to be useful.

Offer to run the install command *with their OK* (it's a `brew`/package
change — rule 2). Don't proceed until Python ≥ 3.11, tmux, and pipx are present.

## Step 1 — Install crr (safe; do it)

```sh
pipx install "git+https://github.com/InfiniteInsight/Claude-Remote-Rescue"
# OR, if the user has the wheel file instead:
# pipx install ./claude_remote_rescue-0.1.0-py3-none-any.whl
crr --version
crr doctor
```

Show the user `crr doctor`'s output and note anything it flags (missing tmux,
PATH issues, etc.). Fix what's safely fixable; surface the rest.

## Step 2 — Shell shim (NEEDS confirmation: edits their rc file)

The shim is a dependency-free snippet that wraps `claude` so crr can track
sessions. Generate it (safe), then propose the one-line rc edit and **wait**.

```sh
# pick the file matching `echo $SHELL` from Step 0:
crr shim zsh  > ~/.crr-shim.zsh        # zsh  (macOS default)
crr shim bash > ~/.crr-shim.bash       # bash
crr shim fish > ~/.config/fish/conf.d/crr.fish   # fish: this dir auto-loads; no rc edit needed
```

For zsh/bash, tell the user you'd like to add ONE line to their rc, and why
(this is the only change to their shell config; it's undone by deleting the
line):

```sh
echo 'source ~/.crr-shim.zsh' >> ~/.zshrc     # or ~/.bashrc for bash
```

After they confirm and you run it, tell them: **open a new terminal** for the
shim to take effect. (fish needs no rc edit — just a new shell.)

## Step 3 — Background agents: watchdog + dashboard (NEEDS confirmation)

First **print** the service definition for review (safe, changes nothing):

```sh
crr launchd        # macOS: prints two launchd user-agent plists
# crr systemd      # Linux: prints the watchdog timer + dashboard unit
```

Summarize for the user what it installs: a ~1-minute **watchdog** that revives
crashed sessions, and the **dashboard** on `127.0.0.1:8377`. On macOS these are
*user* agents — they run only while the user is logged in (that's expected).
On their **yes**:

```sh
crr launchd --install                              # macOS   (Linux: crr systemd --install)
launchctl list | grep claude-remote-rescue         # macOS: expect .revive and .web loaded
```

## Step 4 — Hand off to the user (they drive the actual test)

You've done the install. The real test needs a human at the keyboard. Tell them:

1. Open the dashboard: `open http://127.0.0.1:8377` (macOS) — leave it visible.
2. Run `claude` in a terminal and chat briefly. A card should appear on the
   dashboard within a few seconds.
3. **Crash it:** close that terminal window. Within ~1 minute the watchdog
   should revive the conversation into tmux and the card should flip to a
   recoverable/revived state. Check: `tmux ls` and `tmux attach -t crr-<id>`.
4. **⚠️ The #1 thing to confirm on macOS:** a couple of minutes later (after the
   1-minute revive agent has finished), run `tmux ls` again — **does the
   revived session still exist?** On Linux it does; on macOS this is unverified
   and, if it vanishes, is a real bug (needs `AbandonProcessGroup` in the
   plist). Report the answer either way.
5. Try the dashboard buttons (Kick / Close / Reopen), the **Search** bar, and —
   if any "Discoverable" sessions appear — **Adopt** / **Take over**.

The full test checklist, if you want more detail, is in
[docs/TESTING-macos.md](TESTING-macos.md) §4.

## Step 5 — Build the report for crr's author (do it, hand it to the user)

Run these and collect the output (run the foreground ones briefly, then stop):

```sh
crr doctor
crr web --port 8378        # foreground dashboard on a spare port — shows stderr; Ctrl-C after ~5s
crr revive                 # one foreground watchdog pass — watch its output
log show --last 10m --predicate 'process == "crr"' --info    # macOS unified-log fallback
launchctl print "gui/$(id -u)/com.claude-remote-rescue.web"  # macOS agent status/last exit
```

Then write a short summary: crr version + OS + shell; `crr doctor` result;
anything that **errored** (paste it verbatim); and one line on each of the Step
4 checks (worked / failed / weird), **especially the revived-session-survival
check**. Hand this summary to the user to send back to crr's author.

## Uninstall (clean — leaves the machine as it was)

```sh
crr launchd --uninstall                 # macOS (Linux: crr systemd --uninstall)
# remove the 'source ~/.crr-shim.zsh' line from ~/.zshrc, then:
rm -f ~/.crr-shim.zsh
pipx uninstall claude-remote-rescue
```
