# restore-prompt after a reboot — offer to open crr-restored conversations in tabs

**Status:** design · 2026-08-15
**Issue:** #30 (the deferred "reopen previously-open tabs" UX complaint)
**Scope:** small, self-contained — fix the selection and the action of the
existing `crr rescue-check` / `crr rescued` mechanism. No new command, no
config key, no payload/contract change.

---

## Why

With `reachable-at-boot` deployed, a Windows Update reboot is survivable: WSL
comes up headless, and `crr-revive` re-parks the pre-reboot conversations into
detached tmux sessions in the background. So by the time the builder comes
back and opens WSL, the conversations are already alive — but **hidden**
(running in tmux sessions with no window). Today the builder must reopen each
one by hand (dashboard, or remember every directory and `claude --resume`).

crr already has a mechanism for exactly this: `crr rescue-check`, which the
shims run once per boot on the first interactive shell, prints
*"crr: N conversation(s) … Open them in terminal tabs? [Y/n]"*. **It has been
dead since #58.** Two defects, both verified on the live machine (`crr rescued`
returns "no rescued sessions" while 7 conversations sit parked):

1. **Wrong selection.** `rescue.rescued_sessions()` only offers entries whose
   `boot_id != current_boot`. But when the reviver restores a conversation it
   re-keys the entry onto the live tmux pane pid and stamps
   `boot_id = current` (`reviver._rekey_onto_live_pid`, #58 — so the dashboard
   shows Kick/Close on a live pane). Restoring a conversation is therefore the
   exact act that removes it from the prompt's candidate set.

2. **Wrong action.** The `[Y]` path calls `ops.detmux`, which opens the tab
   **and then untracks** the conversation ("crr no longer manages it"). That
   drops it off the dashboard and out of the reviver's safety net — so it
   would NOT be rescued after the next reboot. This contradicts #33's
   principle ("if I wanted it untracked, I would untrack it").

## What "restored, not yet opened" actually is

The state that describes a conversation crr rescued but the user hasn't opened
yet is not a boot-id comparison — it is:

> **parked in a live tmux session, with no client attached.**

- *parked in a live tmux session* = the reviver put it there and it's alive.
- *no client attached* = the user has not opened a window on it yet.

Both signals already exist: `tmux.list_sessions()` (live names) and
`tmux.attached_sessions()` (the subset with a client attached — built for #32).
The "restored, awaiting a tab" set is `live_tmux − attached_tmux`, intersected
with the journal entries that name those sessions.

## The change

### 1. Selection (core — `crr/core/rescue.py`)

Rewrite `rescued_sessions` to key on parked-and-unattached, dropping `boot_id`:

```
def rescued_sessions(entries, live_tmux, attached_tmux) -> list[dict]:
    out = [
        dict(e) for e in entries
        if e.get("claude") is not None
        and e.get("tmux_session")
        and e["tmux_session"] in live_tmux
        and e["tmux_session"] not in attached_tmux
    ]
    return sorted(out, key=lambda e: e["pid"])
```

- Signature changes `(entries, current_boot, live_tmux)` →
  `(entries, live_tmux, attached_tmux)`. Internal only: `crr.cli` is the sole
  caller (`_cmd_rescued`, `_rescue_check`).
- `claim_prompt` / `already_prompted` / the once-per-boot marker are unchanged.

### 2. Action (cli — `_rescue_check`)

On `[Y]`, call `ops.reopen` for each candidate instead of `ops.detmux`.
`reopen` on a parked entry attaches a tab to the tmux session and **keeps the
entry tracked** (the same op the dashboard's Reopen button uses); once the tab
is up, #32 renders the card as `attached`. `reopen`'s signature needs the
extra dependencies it already takes elsewhere in cli (`controller`, `flags`,
`grace`, `remote_control`, `tab_spawner`, `tabs_expected`) — assembled the
same way `_cmd_reopen` does.

### 3. Both readers resolve the attached set

`_cmd_rescued` and `_rescue_check` each already resolve `live` via
`tmux.list_sessions()`; add a sibling `tmux.attached_sessions()` read and pass
both to `rescued_sessions`. Tri-state honesty (F16): an unknown `live` OR
unknown `attached` (None) degrades to **offer nothing** — never a false
"restored" claim. (`list_sessions() is None` already degrades to `set()` with
a stderr note; `attached_sessions() is None` degrades the same way — an
unreadable attached-state must not make a parked session look unopened.)

### 4. Wording

Prompt: `crr: N conversation(s) restored after the last reboot. Open them in
terminal tabs? [Y/n] `. Headless notice (no tab spawner):
`crr: N conversation(s) restored after the last reboot — 'crr rescued' lists
them; attach with: tmux attach -t <name>`.

## What stays the same (chosen by the builder)

- **Once per boot, first interactive shell.** The atomic O_CREAT|O_EXCL marker
  claim, the tty gate, and the timeout/Ctrl-C = "not now" behavior are kept
  verbatim. A decline or a background shell claiming the marker means no
  re-prompt this boot.
- **Recovery path.** `crr rescued` (now using the corrected selection) lists
  the restored set on demand, and the dashboard Reopen button reopens
  individually — the fallback for "I said not now" or "I want just one".
- **All-or-nothing.** `[Y]` opens every restored conversation in a tab (7 → 7
  tabs); selective handling is what `crr rescued` + dashboard Reopen are for.

## Testing

- **Core (`rescued_sessions`), pure, exhaustive:** a parked-unattached entry is
  offered; an attached one is excluded; an entry whose tmux_session is not in
  `live_tmux` is excluded; a claude-less entry is excluded; ordering by pid;
  an empty/`set()` attached set offers all parked. No boot_id in the predicate.
- **cli `_rescue_check`:** with a fake tmux (live set + attached set) and a
  fake tab spawner, `[Y]` (a) opens a tab per candidate and (b) leaves each
  entry **journaled** (reopen keeps tracking) — asserting it does NOT archive/
  delist the way the old detmux path did. `[n]`/timeout opens nothing. The
  once-per-boot marker still guards a second invocation.
- **Tri-state:** `list_sessions()`→None and `attached_sessions()`→None each
  make the check offer nothing (no prompt, no marker consumed inappropriately).
- **No machine contact:** every test drives injected fakes; nothing registers
  a real task, attaches a real tmux, or reboots.

## Out of scope

- Re-arming the prompt across shells within a boot (the builder chose
  once-per-boot).
- A `crr rescued --open` / dashboard "Open all restored" button (the on-demand
  approach was considered and not chosen; the per-session Reopen button
  already covers deliberate reopening).
- Any change to how the reviver restores or re-keys conversations (#58 stays;
  this spec adapts the prompt to it, not the other way around).
