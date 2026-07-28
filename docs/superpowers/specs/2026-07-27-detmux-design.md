# De-tmux (task #10) — re-home a revived session into a visible tab

**Date:** 2026-07-27
**Status:** approved design (resolved autonomously per the handoff; no open
questions)
**Task:** #10 — the dashboard "de-tmux" button, previously gated behind #8
(done)

## Goal

A revived session lives in a **detached tmux session** (`crr-<sid8>`) —
durable but invisible. The user wants it back in front of them: a visible
terminal tab attached to that session, and crr's bookkeeping updated so the
card stops presenting the session as tmux-parked.

## The operation (`detmux`)

One new classifier-independent op, `ops.detmux`:

```
def detmux(store, archive, tmux, pid, now, *, tab_spawner) -> OpResult
```

1. Read the journal entry (missing/corrupt → `no session <pid>`).
2. The stored `tmux_session` field must be set (else
   `session <pid> is not tmux-parked`). **The stored name is used as-is** —
   it IS the bookkeeping being retired; recomputing `session_name(entry)`
   would silently diverge for pre-rename history.
3. **Liveness comes from tmux, not the field** (`[lesson]` in reviver.py): if
   the name is not in `tmux.list_sessions()`, refuse with
   `tmux session <name> is gone` and touch nothing (the reviver already
   treats the field as untrusted, so a stale field is inert; clearing it on a
   refusal would make a read-only failure path mutating).
4. A tab spawner is **required** (unlike `reopen`, where the tab is a
   best-effort convenience on top of an already-durable revival — here the
   tab IS the operation): `tab_spawner is None or not available()` →
   `no terminal tab spawner available on this host`.
5. `tab_spawner.open_tab(attach_argv(name))` — the existing
   `["tmux", "attach", "-t", name]` argv. A spawn failure → op fails,
   **bookkeeping untouched** (nothing happened; the card must keep offering
   the button).
6. Success → archive the entry (reason `"detmuxed"`) when it carries a
   claude session (mirroring `dismiss`), then **delist it**
   (`store.remove(pid)`), and
   `OpResult(True, "de-tmuxed <pid>: attached <name> in a tab; crr no longer manages it")`.

**Why delist rather than just clear the field** (revised after the final
whole-branch review): the `tmux_session` field is *owned by the reviver* —
its reset branch re-writes `tmux_session = name` on the next pass whenever
the tmux session is live, so a cleared field is re-parked within one
watchdog interval (~30 s) and the De-tmux button reappears; worse, when the
user later exits claude in the attached tab, the reviver would respawn the
conversation as a fresh detached revival. A re-homed session must leave the
reviver's domain entirely. Delisting (with an archive record for
provenance) is the only honest "crr stops managing this" that needs no
journal-schema bump: the card disappears, the reviver can neither re-park
nor resurrect, the tmux session keeps hosting the process under the user's
control, and the tmux session ends naturally when claude exits in the tab.
The trade-off is explicit and intended: re-homing ends crr's rescue net
for that conversation — the user took manual ownership.

What "drop the tmux wrapper" honestly means here: crr stops tracking the
session; the tmux server keeps hosting the process until claude exits
inside it (at which point the tmux session ends and the tab closes with
it). Physically re-parenting a process out of tmux is not possible;
killing + respawning claude in a plain tab was considered and rejected (it
races two claudes on one conversation or drops the running one — and the
handoff prescribes the attach shape). CLI-only (no button) was rejected
because #10 *is* the dashboard button.

`detmux` mutates the journal, so both surfaces run it under the mutation
lock, like every other mutating op.

## Surfaces (one gate, both surfaces — mirrors reopen/kick/close)

- **CLI:** `crr detmux <pid>` — lock, call `ops.detmux`, print message,
  rc 0/1 (1 = op refusal; 2 = tmux binary missing, matching reopen's
  boot/tmux-missing convention where applicable).
- **Web:** `ACTIONS` gains `"detmux"`; `action_provider` branch calls the
  same op with the `web` command's already-built `tmux_spawner` and `tab`
  spawner.
- **Dashboard:** the card gains a **De-tmux** button, shown only when the
  card's `tmux_session` field is non-null (any state — the field is the
  user-visible fact; the op re-verifies liveness server-side). Placed with
  the non-destructive actions (not `danger`).

## Contract impact (versioned, per AGENTS.md)

- The sessions card gains `tmux_session` (nullable string) →
  **`SESSIONS_CONTRACT_VERSION` 2 → 3**, `SESSION_CARD_KEYS` +
  `validate_session_card` updated, tests updated (v2 payloads must now be
  rejected by the validator's version check).
- **`PAGE_VERSION` bump** (new button + gating logic in `page.html`); the
  `node --check` page-JS gate still applies.
- `tmux_session` is crr-generated (`crr-<8hex>`, metacharacter-free) but the
  dashboard still renders exclusively via `textContent`/button-gating —
  nothing reaches innerHTML (project rule; no exceptions for "trusted"
  fields).
- No journal schema change (`tmux_session` already exists there).

## Testing

- `test_ops.py`: detmux happy path (tab argv = `tmux attach -t <name>`,
  entry archived with reason `detmuxed`, entry delisted — so the reviver
  can never re-park or resurrect it); a claude-less parked entry (pid-reuse
  shape) delists without an archive record; refusals: no entry / no
  `tmux_session` / name not live in tmux / spawner missing / spawner
  unavailable; spawn raises → failure AND the entry fully preserved. Fakes
  already exist (FakeTmux, FakeTab patterns in test_ops.py).
- `test_contracts.py`: v3 card round-trip, wrong-version rejection,
  `tmux_session` nullable-string typing.
- `test_status.py`: card carries `tmux_session` from the entry.
- `test_cli.py`: `crr detmux` wiring (mirrors kick/close CLI tests).
- `test_web.py`: `detmux` accepted by `/api/action` validation and routed;
  unknown ops still 400.
- `node --check` gate green after the `page.html` change.

## Non-goals (YAGNI)

- No "kill the tmux session after attach" cleanup — the session ends
  naturally when claude exits; killing early risks the running conversation.
- No new spawner adapters; whatever `_tab_spawner(config)` selects today is
  what detmux uses (on a host with no tab adapter the op refuses honestly).
- No macOS/Windows-specific live verification in this task (unit-level
  parity only, same as every adapter-backed op; the spawners themselves are
  already unit-tested cross-OS).
