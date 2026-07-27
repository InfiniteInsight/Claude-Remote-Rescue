# kick / close commands + shim repair loop (task #4)

**Date:** 2026-07-27
**Status:** approved design, pre-implementation
**Task:** #4 — the last session-operation gap before the cutover

## Goal

Give crr the three live-session operations ccresume shipped, at **full
feature parity** and **cross-OS from the start** (Linux, WSL, macOS; Windows
via WSL per the project's Phase-4 model):

- **`kick`** — restart claude *in place*, on the same conversation.
- **`close`** — the remote equivalent of typing `exit`: end a live session,
  no revival.
- **shim repair loop** — the `claude()` wrapper resumes after an unexpected
  exit: *silently* when a kick armed it, or by *offering* on a bare crash.

`reopen` / `dismiss` / `remove` already exist and are unchanged. This closes
the operation set the DESIGN enumerates ("Session operations (all
classifier-gated, pid-keyed)").

### Parity is a hard requirement

The OSS cross-OS rewrite must match the original tool feature-for-feature.
For #4 that means: kick, close, and the repair loop are **implemented and
unit-tested on every supported OS and every supported shell in the same
change** — not staged behind hardware. The only thing that legitimately
awaits hardware is the *live* end-to-end run on a physical Mac; the code and
its tests are portable and complete regardless. Calibration language stays
honest: "implemented + unit-tested cross-OS; live-verified on Linux/WSL."

## Non-goals (YAGNI)

- No native (non-WSL) Windows process control — Windows is WSL here.
- No new "kill by cmdline pattern" path — explicitly forbidden by DESIGN
  (`[lesson: kill-by-ancestry]`).
- No change to `reopen` / `dismiss` / `remove` semantics.
- No interactive picker in the repair loop beyond the single `[Y/n]` offer.

## The crux: what kick/close kill

**Kill by ancestry, never by cmdline.** The obvious alternative — have the
shim record claude's pid at launch — does not work: claude is a foreground
TUI and cannot be backgrounded to capture its pid without losing the
controlling terminal it needs.

So the controller finds the target by process ancestry:

1. Start from the journaled **shell pid**.
2. `ps` scan (`-o pid=,ppid=,pgid=`, portable across GNU and BSD ps) → find
   the child process(es) of the shell whose process group differs from the
   shell's own group. Under job control, `command claude` runs as its own
   group leader, so that group is claude (plus any node/subprocess children
   it spawned — all sharing claude's pgid). Normally exactly one such group
   exists; if several do, signal **each** non-shell child group.
3. Signal **the whole process group** (`kill -SIG -<pgid>`), never the shell.

If the shell has no such child (claude already gone), the op reports that and
does nothing.

### Signals and the grace window (parity)

Both `kick` and `close` send **SIGTERM** first (graceful; claude flushes its
transcript). If the group is still alive after a configurable grace window
(`close_grace_seconds`, default 5), escalate to **SIGKILL**. This mirrors the
"close/reopen grace windows" the DESIGN's config section calls out.

The single difference between the two ops: **`kick` arms a relaunch flag
before signalling; `close` does not.**

## The relaunch flag protocol

A flag is the one bit of shared state between the `kick` op and the shim's
repair loop.

- **Location:** `<state_dir>/relaunch/<shell_pid>`, content = the
  `session_id` to resume. New directory under the state dir the core journal
  already owns.
- **Armed by `kick`, only when the kill lands:** write the flag → signal →
  **roll the flag back (unlink) if the signal failed**, returning an error.
  This satisfies DESIGN's "relaunch flags are written only when a kill
  actually lands" while still having the flag present *before* the shim wakes
  (avoiding the race where the wrapper checks before kick writes).
- **Cleared at wrapper start:** the shim unlinks any stale flag for its pid
  before running claude, so a flag from a session the user later closed on
  purpose can never silently resume it (`[lesson: flag files]`).
- **Consumed + cleared by the repair loop** after it reads it.

## Architecture (respects `cli → adapters → core`)

### New port — `ProcessController` (`core/ports.py`)

Separate from the read-only `ProcessProbe` (signalling is a mutation; a
caller that only reads should not get signal power):

```
class ProcessController(Protocol):
    def claude_group(self, shell_pid: int) -> int | None: ...   # pgid or None
    def signal_group(self, pgid: int, sig: int) -> None: ...
    def group_alive(self, pgid: int) -> bool: ...               # for grace escalation
```

### Real adapter — `PsProcessController` (`adapters/process_probe.py`)

Extends the existing ps machinery in the same file. Portable across
Linux/WSL/macOS. Command *shapes* are pure builders (unit-tested without a
real `ps`); the one method that shells out is covered like the existing
probe.

### New core module — `FlagStore` (`core/flags.py`)

`arm(pid, sid)` / `read(pid) -> str | None` / `clear(pid)`, atomic writes to
`<state_dir>/relaunch/`. Pure core file I/O, consistent with `journal.py`
(core already owns the state-dir filesystem). No new versioned contract — a
flag is an opaque marker keyed by pid; its *content* is a sid the journal is
already the source of truth for.

### Core ops (`core/ops.py`) — fill the existing kick/close stub

```
def kick(store, controller, flags, boot, probe, pid, now, *, grace) -> OpResult
def close(store, controller, boot, probe, pid, now, *, grace) -> OpResult
```

- **Classifier-gated to `live`/`ghost`** (refuse `crashed` — nothing is
  running to signal; that is `reopen`'s job). Same gate on both surfaces.
- **`kick` additionally requires a claude session** (`claude is not None`) —
  it reads the sid from the journal entry to arm the flag; a claude-less
  shell has nothing to relaunch, so kick refuses it. `close` needs no sid.
- `kick`: read sid from entry → resolve group → arm flag → SIGTERM → grace →
  SIGKILL if alive → roll flag back on total failure. Returns `OpResult`.
- `close`: resolve group → SIGTERM → grace → SIGKILL if alive. No flag.
- Failure statuses **propagate** (the swallowed-exit-code → green-checkmark
  lesson).

### CLI + web

- `crr kick <pid>` / `crr close <pid>` handlers calling `ops.*`.
- Two new `/api/action` ops (`kick`, `close`) added to `ACTIONS`, same
  strict validation, calling the same `ops.*` — one gate, both surfaces
  (mirrors reopen/dismiss/remove).
- Dashboard buttons: **Kick** + **Close** on `live`/`ghost` cards
  (`PAGE_VERSION` bump). Kick/Close never shown on `crashed` cards (Reopen is).

### Shim repair loop (fish, bash, zsh — all three, together)

The `claude()` wrapper gains a loop around `command claude`:

1. **Start:** clear any stale relaunch flag for this pid.
2. Run `command claude …`; capture exit code.
3. **If the relaunch flag is set** (kick): silently
   `command claude --resume <sid>`; loop.
4. **elif exit code is nonzero** (crash): print
   `crr: claude exited unexpectedly (<code>). Resume this conversation?
   [Y/n]` and read the answer. **yes / timeout / no-tty → resume**; explicit
   **no → stop**. Loop on resume. (Timeout uses the shell's timed read where
   it has one — bash/zsh `read -t`; fish lacks a native timed read, so it
   falls back to a blocking read and relies on the no-tty→resume rule for the
   unattended case.)
5. **else** (clean exit): stop.
6. **Bounded to 2 resume attempts** per invocation — a session that keeps
   dying gives up in place (the shim analogue of the reviver's give-up
   guard) instead of spinning, then falls through to `claude-exit`.

Small shim-facing helpers back this: `crr relaunch-flag --pid --check` /
`--clear` (presence + sid), reusing the journal for the sid. Behaviour is
identical across the three shells; only syntax differs.

## Contracts / versioning impact

- **No journal schema change** — the flag lives outside the journal.
- **No new sessions/diagnostics contract** — cards gain buttons, not fields.
- **`PAGE_VERSION`** bumps (new Kick/Close buttons in `page.html`).
- New CLI subcommands + two new `ACTIONS` entries.

## Testing strategy

**Slice 1 (server side) — fully unit-testable with fakes, no shell, no real
processes:**
- `PsProcessController` builders: ppid/pgid parsing, group selection, signal
  argv shape (`-<pgid>`), grace escalation logic — pure, cross-OS.
- `FlagStore`: arm/read/clear, atomicity, stale isolation.
- `ops.kick`/`ops.close`: classifier gate (refuse crashed), flag arm +
  rollback-on-signal-failure, close-arms-no-flag, failure propagation — with
  fake controller/probe/boot.
- CLI + web: new commands, new `/api/action` ops, strict validation.

**Slice 2 (shim repair loop) — `test_shims.py`, gated per installed shell:**
- stale-flag cleared at start; kick-flag → silent resume; nonzero → offer
  (drive `yes`/`no`/timeout/no-tty); clean exit → no resume; 2-attempt cap.
- Live verification on Linux/WSL (fish); macOS live run awaits hardware.

Every step is test-first (red → green), advisor before/after each slice,
`PAGE_VERSION` discipline, honest calibration.

## Delivery — two mergeable slices

1. **Server side**: port + `PsProcessController` + `FlagStore` +
   `ops.kick/close` + CLI + web endpoints + dashboard buttons. Merges on
   local-CI-green; delivers working dashboard Kick/Close on live sessions and
   flag-arming, with zero shell risk.
2. **Shim repair loop**: the shell code in all three shims consuming the flag
   + offer-on-crash, `test_shims.py` coverage, live Linux/WSL verification.

The cutover (moving `cc-*` under crr's shim) is a **separate** operational
plan authored after #4 lands.

## Slice-2 blockers surfaced by Slice-1's final review

Two protocol/semantic gaps the shim-repair-loop slice MUST resolve (they are
not Slice-1 code bugs — the loop does not exist yet — but the flag protocol
Slice 1 establishes cannot express them):

- **B1 — the flag cannot distinguish `close` from a crash.** `terminate_group`
  makes claude exit nonzero (SIGTERM→143 or SIGKILL→137). The repair loop's
  rule is "nonzero exit → offer, resume on yes/timeout/no-tty," so an
  unattended `close` would be resumed — violating the acceptance criterion
  "`crr close` … no relaunch." Resolution options: (a) `close` arms a
  *suppress-resume* marker the wrapper honours (the flag grows a second
  state: relaunch vs. do-not-resume), or (b) the wrapper treats the
  close-range exit codes as clean. Pick one in the Slice-2 design.
- **B2 — after a successful `close`, the shell survives, so the session
  still classifies `live`/`ghost`** and the card persists with Kick/Close
  still shown (a second `close` returns "no running claude process found").
  Decide the intended semantics — does `close` also end the shell (true
  "remote exit"), or is it "kill claude, leave the shell"? — and align the
  button label/behaviour accordingly.

## Acceptance criteria

- `crr kick <pid>` on a live session: claude's group dies, the flag is armed,
  and (under crr's shim) claude comes back on the same conversation; on a
  crashed session it is refused.
- `crr close <pid>` on a live session: claude's group dies gracefully, no
  relaunch; on a crashed session it is refused.
- Repair loop: kick → silent resume; crash → offer, resume on yes/timeout,
  stay dead on no; user-quit (clean exit) → never resumes; ≤2 attempts.
- Kill is always by process group of the claude child, never by cmdline.
- Implemented + unit-tested on Linux/WSL/macOS and fish/bash/zsh; live-run on
  Linux/WSL. No feature is Linux-only.
