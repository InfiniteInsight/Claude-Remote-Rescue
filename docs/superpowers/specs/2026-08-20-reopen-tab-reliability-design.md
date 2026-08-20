# Reopen Tab Reliability Design

**Date:** 2026-08-20
**Status:** Draft
**Motivation:** Sessions should always have a tab on the laptop. Two gaps
today prevent that: (1) LIVE sessions on the dashboard have no Reopen
button, so a session restored into detached tmux after a reboot can't be
tabbed from the phone; (2) the boot-time rescue prompt fires once and is
easy to miss, leaving restored sessions alive but invisible.

## Problem

After a reboot the reviver restores conversations into detached tmux
sessions. They classify as LIVE (claude is running in the pane). But
there is no terminal tab attached to them, and:

- **On the dashboard:** LIVE sessions show Kick / Close but no Reopen.
  The user sees the session alive on the phone but has no button to get a
  tab on the laptop. Only `crashed` and `parked` states offer Reopen.
- **On the laptop:** `rescue-check` fires on the first interactive shell
  with a [Y/n] prompt. If the user misses it, answers 'n', or the shell
  is non-interactive, the per-boot marker is already written and no later
  shell re-offers. The recovery path (`crr rescued` + manual
  `tmux attach`) requires knowing the session names.

## Changes

### 1. Extend `ops.reopen` to LIVE sessions

**Current:** `ops.reopen` refuses LIVE sessions with
`"session {pid} is live — use kick or close"`, except for the PARKED
sub-case where the journaled pid IS the tmux pane's process (it already
calls `_open_tab` for that branch).

**New:** When `ops.reopen` encounters a LIVE session that has a
`tmux_session` name in the journal entry, it skips revival and calls
`_open_tab` to attach a terminal tab to the existing tmux session.

- No spawn, no kill, no archive — only tab creation
- Returns `OpResult(True, "opened tab for {name}",
  degraded=tabs_expected and not landed)`
- The existing PARKED path (which already handles LIVE-with-tmux) is
  subsumed by this generalization: any LIVE session with a tmux home
  gets tab-attach-only

**page.html:** Add `addBtn("Reopen", "reopen", false, false)` to the
LIVE state's action button set. Guard on `s.tmux_session` being truthy
(a LIVE session without a tmux session name can't be tabbed). Ghost
state already has Restore (which calls reopen); no change there.

### 2. Auto-open tabs on first interactive shell after reboot

**Current:** `_rescue_check` finds restored-but-unattached sessions,
prompts [Y/n], and opens tabs on 'Y'. A per-boot marker prevents
re-firing. Timeout or 'n' writes the marker, closing the window.

**New:** Remove the [Y/n] prompt. When restored-but-unattached sessions
are found, open tabs immediately. The per-boot marker is written after
tabs are opened (or attempted), so subsequent shells don't re-fire.

- **Config key:** `rescue_auto_open` (default `true`). When `false`,
  fall back to the current [Y/n] prompt behavior.
  `CONFIG_DEFAULTS_VERSION` bumps 20 → 21.
- **Non-interactive shells:** Still skip silently, no marker written (so
  the first truly interactive shell fires).
- **No tab spawner available:** Still degrades to the notice message
  (`"N conversation(s) restored — 'crr rescued' lists them"`).
- **Headless hosts (no tabs_expected):** Still use `_terminal_reopen`
  to link windows into the current tmux session or build the aggregate.
  The auto-open bypasses the prompt, but the tmux-link flow is unchanged.

### What doesn't change

- The reviver's detached-tmux-first-then-tab strategy
- The `degraded` flag and "NO TAB" warning (both dashboard and CLI)
- Tab spawner detection (WSL/Linux/macOS)
- The `crr reopen <pid>` CLI command (it already has the full flow;
  the ops.reopen change affects it too — a `crr reopen` on a live
  session will now open a tab instead of refusing)
- `_open_tab`'s best-effort semantics (timeout, ENOEXEC, etc.)
- Rescue-check's per-boot marker mechanics (the timing of when the
  marker is written shifts, but the mechanism is the same)

## Layering

Both changes stay within the existing architecture:

- `ops.reopen` is pure core (ports + stores); the LIVE-tab-attach
  path adds no new dependencies
- `_rescue_check` is CLI (it already calls `ops.reopen` and
  `_terminal_reopen`); the auto-open removes a code path (the prompt)
  rather than adding one
- Config key follows the established pattern (DEFAULTS + version bump +
  ledger comment)
- No new adapters, no new ports, no new modules

## Testing

### ops.reopen LIVE-with-tmux
- LIVE session with `tmux_session` set: returns ok, calls `_open_tab`,
  does not spawn a new tmux session
- LIVE session without `tmux_session`: refuses (unchanged behavior for
  a LIVE shell that isn't tmux-managed)
- LIVE session, tab spawner unavailable: returns ok + degraded
- LIVE session, tab spawner timeout: returns ok + degraded

### rescue-check auto-open
- `rescue_auto_open=true` (default): restored sessions get tabs without
  prompting; marker is written
- `rescue_auto_open=false`: prompt behavior is preserved (backward compat)
- Non-interactive shell: no marker written, no tabs opened
- No tab spawner: notice printed, no error
- Multiple shells: first writes marker, second is a no-op

### page.html
- LIVE cards with `tmux_session` show Reopen button
- LIVE cards without `tmux_session` do not show Reopen
- PAGE_VERSION bumps; page version guard updated

## Config

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `rescue_auto_open` | bool | `true` | Skip [Y/n] prompt and auto-open tabs for restored sessions on boot |

## Scope

This is a focused reliability fix — two narrow changes to existing
paths. No new modules, no new adapters, no new dashboard panels. The
dashboard page.html change is a single `addBtn` line plus a guard.
