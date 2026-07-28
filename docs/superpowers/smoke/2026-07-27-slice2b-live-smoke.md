# Slice 2b live smoke test — isolated, Linux/WSL, fish

**Date:** 2026-07-27 · **Branch:** `feat/shim-repair-loop` · **Run by:** the
autonomous completion session (main session, never a subagent, per the
handoff's safety rules §0).

## Isolation

Everything ran under scratch state — production was never touched:

- `XDG_STATE_HOME` → session-scratchpad `smoke/state`
- `TMUX_TMPDIR` → `/tmp/claude-1000/crr-smoke` (own tmux server/socket; the
  scratchpad path exceeded the unix-socket 108-char limit, so the socket
  lived in a short scratch dir instead — still fully separate from the
  default socket)
- fake `claude` first on `PATH`: records argv, `exec sleep 1000000`
  (crash/kick/close scenarios) or `exec sleep 2` (clean-exit scenario)
- `fish --no-config` sourcing a freshly generated shim
  (`crr shim fish --crr-bin <repo>/.venv/bin/crr`)
- stray detection by `ps -C sleep` (name match), never `pkill -f`

Production check before AND after: the 7 live `cc-*` tmux sessions on the
default socket, `ccresume-web` listening on 127.0.0.1:8377, and zero foreign
`sleep` processes — identical both times.

## Scenarios and results

| # | Scenario | Result |
|---|----------|--------|
| 1 | **kick → silent resume** — `crr kick <shell_pid>` against the scratch session | PASS: old claude group (pgid 407061) died; wrapper relaunched with `--resume f1994a70-…` (same injected sid); no prompt in the pane; new group appeared |
| 2 | **close → shell exits** — `crr close <shell_pid>` | PASS: wrapper ran `claude-exit` and `exit`; the fish shell died, the tmux window (and with it the scratch server's last session) closed; journal entry deregistered; record shows no extra launch |
| 3 | **crash → offer** — `kill -9 -<pgid>` directly (no flag) | PASS: `crr: claude exited unexpectedly (137). Resume this conversation? [Y/n]` visible in the pane; explicit `n` → no resume, `claude` journal field cleared, shell back at prompt. Repeated with `y` → `--resume <sid>` launched |
| 4 | **clean → return** — fake claude exits 0 | PASS: wrapper returned to the prompt; no offer, no resume (record has exactly 1 launch); `claude` journal field cleared |

## Finding fixed during the smoke (would have shipped otherwise)

The unit tests assert the offer text lands on stderr — which it did — but in
a real interactive pane fish's `read` builtin **repaints its `read>` prompt
on the current line**, erasing the same-line question: the user saw only a
bare `read>`. Fixed in `crr/shims/crr.fish` by newline-terminating the offer
line (bash/zsh `read` does not repaint; they keep the same-line prompt).
Scenario 3 re-run after the fix: the offer text is visible. All fish unit
tests re-run green after the fix (10/10 `-k "repair and fish"`).

## Calibration

Live-verified on Linux/WSL with fish. bash and zsh are unit-tested (bash
executed on this machine, 11/11; zsh code reviewed statically — zsh is not
installed here, its tests will run on the first machine that has it). macOS
live runs await hardware.
