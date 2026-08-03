# RUNBOOK — production cutover and first release

> **Update 2026-08-03:** the **cutover is COMPLETE** — crr is now the live
> tool on HedyLamarr (owns port 8377, the watchdog, and the shim; ccresume
> retired as a rollback net). The cutover phases below are **historical
> record**. The **release/publishing** phase (GitHub release/tag,
> Homebrew/AUR, announcement) is **still pending** and remains human-gated.

**Status:** written by the autonomous completion session, deliberately **not
executed** — both procedures are human-gated: the cutover disrupts your live
sessions and must be done with you present; publishing needs your accounts
and a real release. Every phase has a rollback. Do the phases in order and
verify each before the next.

Facts this runbook was written against (2026-07-27):

- Production: **7** live `cc-*` tmux sessions (the handoff said 6 — recount
  before starting), `ccresume-web.service` on **127.0.0.1:8377**,
  `ccresume-watchdog.timer` (1-minute cadence).
- The ccresume fish shim loads from `~/.config/fish/conf.d/ccresume.fish`.
- crr's units: `crr-revive.service` + `crr-revive.timer` +
  `crr-web.service`, installed by `crr systemd --install [--port N]`
  (defaults to port **8377** — during the transition you MUST pass a
  different port; 8378 is used below).
- crr is installed editable at `~/projects/Claude-Remote-Rescue` with the
  CLI at `.venv/bin/crr`. For production, pick the binary you want baked
  into shims/units first (a pipx install survives repo surgery; the .venv
  path is fine if the repo stays put).

---

## Part A — Cutover (Path A): move the live sessions and dashboard to crr

**Why sessions must be cycled:** the `cc-*` sessions are ccresume-journaled
and their shells have ccresume's `claude()` shim *already loaded in
memory*. crr cannot hot-adopt them; each must be restarted under crr's
shim. Conversations are safe throughout — transcripts live on disk and
`claude --resume <sid>` continues them under either tool.

### Phase A0 — snapshot the baseline (5 min, no changes)

```fish
tmux ls | tee ~/cutover-baseline-tmux.txt
systemctl --user list-units 'ccresume*' --no-pager | tee ~/cutover-baseline-units.txt
ss -tlnp | grep 8377 | tee ~/cutover-baseline-port.txt
# For each cc-* session, record the conversation it holds (sid + cwd):
# open the ccresume dashboard (http://127.0.0.1:8377 via tailnet) and note
# every session's id/cwd, or capture its status JSON if it offers one.
```

Keep these files until Phase A5. **Rollback for A0:** none needed.

### Phase A1 — bring crr up SIDE-BY-SIDE (no ccresume changes)

```fish
cd ~/projects/Claude-Remote-Rescue
.venv/bin/pytest -q && .venv/bin/lint-imports        # local CI green first
.venv/bin/crr systemd --port 8378                    # inspect units first
.venv/bin/crr systemd --install --port 8378          # NEVER 8377 here
systemctl --user status crr-web.service crr-revive.timer
curl -s http://127.0.0.1:8378/api/version            # dashboard answers
```

ccresume is untouched; both watchdogs run (they watch different state dirs
and different tmux session name prefixes — ccresume's journal vs
`<XDG_STATE_HOME>/crr`; no interference, but verify Phase A2's canary
before trusting that).

**Rollback A1:** `systemctl --user disable --now crr-revive.timer
crr-web.service crr-revive.service; rm ~/.config/systemd/user/crr-*.{service,timer}
&& systemctl --user daemon-reload`.

### Phase A2 — shim swap for NEW shells only

```fish
.venv/bin/crr shim fish > ~/.config/fish/conf.d/crr.fish
mv ~/.config/fish/conf.d/ccresume.fish ~/ccresume.fish.disabled   # keep it!
```

Existing shells (including all `cc-*` sessions) keep the ccresume functions
they already loaded — they are NOT affected. Only shells started from now
on register with crr.

Canary: open one **new** tab, run `claude` with a throwaway prompt, confirm
a card appears on **8378** (state `live`), quit claude, confirm the card
clears. Try `crr kick <pid>` and watch it silently resume, then `crr close
<pid>` and watch the tab close — this is the repair loop you'll rely on.

**Rollback A2:** `mv ~/ccresume.fish.disabled
~/.config/fish/conf.d/ccresume.fish && rm ~/.config/fish/conf.d/crr.fish`.
New shells go back to ccresume; nothing else changed.

### Phase A3 — cycle the 7 `cc-*` sessions, ONE AT A TIME

Start with the least important session as a second canary. For each:

1. In the session, bring claude to a safe stopping point (no in-flight
   tool runs). Note its sid (from A0's inventory; `sid8` is enough to find
   the transcript).
2. Quit claude cleanly (`/exit` or Ctrl-D at the claude prompt) — this
   flushes the transcript. Then `exit` the shell; the `cc-*` tmux session
   ends. (Dismiss/remove the dead entry on the ccresume dashboard if it
   lingers.)
3. Open a new terminal tab (now crr-shimmed) in the same cwd and resume
   the same conversation: `claude --resume <sid>`.
4. Verify on 8378: the card is there, `live`, correct cwd, correct sid,
   `sid_source` `guessed` upgrading to `verified` within a revive pass.
5. Only then move to the next session.

Notes: whether the new home is a plain tab, tmux, or ssh is your choice —
crr journals `host` either way (the old `cc-*` names belonged to
ccresume; crr's revival naming is `crr-<sid8>` and only appears if a
session later crashes and is revived). If a resumed conversation looks
wrong, STOP cycling further sessions and investigate before touching the
rest.

**Rollback A3 (per session):** the conversation is a transcript on disk —
`claude --resume <sid>` from any shell (even a ccresume-shimmed one via
`fish -c 'source ~/ccresume.fish.disabled; claude --resume <sid>'`, or
just plain `claude --resume <sid>` with no shim at all) gets it back. No
session content is ever destroyed by cycling; the worst case is an
untracked-but-running conversation.

### Phase A4 — dashboard handover

Once all sessions are cycled and 8378 shows them all:

```fish
systemctl --user disable --now ccresume-watchdog.timer   # its journal is empty now
systemctl --user disable --now ccresume-web.service      # frees 8377
```

Then either (a) keep crr on 8378 and update your tailnet
bookmarks/shortcuts — zero further changes — or (b) re-home crr to the old
address: `crr systemd --install` (default port 8377, only NOW that
ccresume has released it), then `systemctl --user restart crr-web.service`
and verify `curl -s http://127.0.0.1:8377/api/version`.

**Rollback A4:** `systemctl --user enable --now ccresume-web.service
ccresume-watchdog.timer` (their unit files were never removed). If crr took
8377, first re-run `crr systemd --install --port 8378` + restart so the
port is free again.

### Phase A5 — retirement (only after days of satisfaction)

Keep ccresume disabled-but-installed as the rollback net until you've been
happy for a while (suggested: a week including one reboot — the #8 test
proved crr survives reboot, but prove it on YOUR real sessions once).
Then, optionally: remove the ccresume units
(`rm ~/.config/systemd/user/ccresume-* && systemctl --user daemon-reload`),
delete `~/ccresume.fish.disabled`, and archive the ccresume install
directory. Nothing forces this step; a dormant rollback net costs nothing.

---

## Part B — Publishing (release, Homebrew, AUR, announcement)

Prereqs: your GitHub account (repo admin), a Homebrew-capable Mac or
Linuxbrew host, an AUR account with SSH key, and an Arch box/container for
`makepkg`.

### B1 — cut the release

```bash
cd ~/projects/Claude-Remote-Rescue
.venv/bin/pytest -q && .venv/bin/lint-imports          # green gate
# Fill in the release date in CHANGELOG.md: change
#   "## [0.1.0] — unreleased"  ->  "## [0.1.0] - YYYY-MM-DD"
git add CHANGELOG.md && git commit -m "chore(release): date 0.1.0"
git tag -a v0.1.0 -m "claude-remote-rescue 0.1.0 — first release"
git push origin main v0.1.0
gh release create v0.1.0 --title "claude-remote-rescue 0.1.0" \
  --notes-file <(sed -n '/## \[0.1.0\]/,/^## /p' CHANGELOG.md | head -n -1)
```

**Rollback B1** (only BEFORE anyone consumed it): `gh release delete
v0.1.0 && git push origin :refs/tags/v0.1.0 && git tag -d v0.1.0`. After
publicizing, never delete a tag — ship 0.1.1 instead.

### B2 — fill the checksums

```bash
curl -L https://github.com/InfiniteInsight/Claude-Remote-Rescue/archive/refs/tags/v0.1.0.tar.gz \
  | shasum -a 256          # on Linux: sha256sum
# Replace BOTH placeholders with the result:
#   packaging/homebrew/claude-remote-rescue.rb  (sha256 "...")
#   packaging/aur/PKGBUILD                      (sha256sums=('...'))
# and regenerate .SRCINFO on Arch later (B4). Commit + push.
```

### B3 — Homebrew

homebrew-core's `brew audit --new` enforces a notability gate (~75 stars);
a fresh repo will likely not clear it. The personal-tap route is identical
for users except the tap name:

```bash
# One-time: create github.com/InfiniteInsight/homebrew-tap with Formula/
cp packaging/homebrew/claude-remote-rescue.rb <tap>/Formula/
cd <tap> && git commit -am "claude-remote-rescue 0.1.0" && git push
brew tap InfiniteInsight/tap
brew audit --strict --online InfiniteInsight/tap/claude-remote-rescue
brew install --build-from-source InfiniteInsight/tap/claude-remote-rescue
brew test InfiniteInsight/tap/claude-remote-rescue
crr --version   # 0.1.0
```

If audit flags `python@3.12` as outdated, switch the `depends_on` to the
current default python formula — one-token edit, no resources to re-pin.
**Rollback:** delete the formula from the tap (users who installed keep
their install).

### B4 — AUR

```bash
# On Arch (container is fine), with your AUR SSH key loaded:
git clone ssh://aur@aur.archlinux.org/claude-remote-rescue.git aur-crr
cp packaging/aur/PKGBUILD aur-crr/ && cd aur-crr
makepkg --printsrcinfo > .SRCINFO      # replaces the hand-written one
makepkg -si                            # full local build + install test
crr --version                          # 0.1.0
git add PKGBUILD .SRCINFO && git commit -m "0.1.0: initial release" && git push
```

Copy the regenerated `.SRCINFO` back into `packaging/aur/` in this repo so
they don't drift. **Rollback:** file an orphan/deletion request, or push a
fixed pkgrel bump — AUR mistakes are cheap.

### B5 — announcement

Your voice, your venues. Raw material: README's feature table, CHANGELOG
0.1.0, DESIGN.md's lessons section. Honest calibration to carry into any
post: unit-tested cross-OS (Linux/WSL/macOS code paths, fish/bash/zsh);
live-verified on Linux/WSL; macOS/Windows live runs still awaiting
hardware; zsh executes in CI/other machines, not yet on the dev box.

---

## Absolute rules that survived into this runbook

- Never `git clean -fdx` in the repo; never `pkill -f` anything — find
  processes by `ps -C <name>` or the journal's pids.
- One session at a time in Phase A3; stop at the first anomaly.
- crr never binds 8377 while ccresume owns it (A1 uses 8378; only A4(b)
  may take 8377, after ccresume releases it).
- ccresume stays installed (disabled) until you decide otherwise in A5.
