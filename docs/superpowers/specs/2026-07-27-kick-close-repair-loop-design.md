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

Both ops kill claude's group identically; they differ only in **which flag
they arm** for the wrapper to act on afterward: `kick` arms **relaunch**,
`close` arms **close**.

## The flag protocol (3-state)

A single flag file is the shared state between the `kick`/`close` ops and the
shim's repair loop. It carries one of two armed states, or is absent — three
outcomes the wrapper reads after claude exits:

| Flag | Armed by | Wrapper acts |
|------|----------|--------------|
| **relaunch**(sid) | `kick` | silently `claude --resume <sid>` — same conversation, shell stays |
| **close** | `close` | run `claude-exit` (deregisters → card vanishes), then **`exit` the shell** → the tab/pane/ssh session closes |
| *(absent)* | — | nonzero exit → **offer** `[Y/n]`; clean exit → return to prompt |

This resolves the two gaps Slice 1's final review surfaced:

- **A closed session is never resumed** — the wrapper sees `close`, not a
  bare crash, so it exits instead of offering/resuming (was blocker B1).
- **`close` ends the whole terminal** — not by an external, signal-fragile
  kill of the shell, but by the wrapper calling `exit` itself: graceful
  (`claude-exit` runs first, so the card disappears and no crashed entry is
  left behind), and uniform across tab / tmux-pane / ssh (was blocker B2).

Protocol details:

- **Location:** `<state_dir>/relaunch/<shell_pid>`, content encodes the kind
  (`relaunch <sid>` or `close`). New directory under the state dir core owns.
- **Armed only when the kill lands:** both ops write the flag → signal the
  group → **roll the flag back (unlink) if the signal failed**, returning an
  error. Satisfies DESIGN's "flags are written only when a kill actually
  lands" while having the flag present *before* the shim wakes (no
  check-before-write race).
- **Cleared at wrapper start:** the shim unlinks any stale flag for its pid
  before running claude, so a flag from a prior action can never act on a new
  launch (`[lesson: flag files]`).
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

### Core module — `FlagStore` (`core/flags.py`)

3-state interface: `arm_relaunch(pid, sid)` / `arm_close(pid)` /
`read(pid) -> (kind, sid|None) | None` / `clear(pid)`, atomic writes to
`<state_dir>/relaunch/`. Pure core file I/O, consistent with `journal.py`
(core already owns the state-dir filesystem). No new versioned contract — a
flag is an opaque per-pid marker.

*(Slice 1 shipped the 1-state form `arm(pid, sid)`; Slice 2's server slice
generalizes it to the 3-state form above and updates `ops.kick` to call
`arm_relaunch`.)*

### Core ops (`core/ops.py`) — fill the existing kick/close stub

```
def kick(store, controller, flags, boot, probe, pid, *, grace) -> OpResult
def close(store, controller, flags, boot, probe, pid, *, grace) -> OpResult
```

- **Classifier-gated to `live`/`ghost`** (refuse `crashed` — nothing is
  running to signal; that is `reopen`'s job). Same gate on both surfaces.
- **`kick` requires a claude session** (`claude is not None`) — it reads the
  sid from the journal entry to arm the relaunch flag; a claude-less shell
  has nothing to relaunch, so kick refuses it.
- `kick`: read sid → resolve group → `arm_relaunch(pid, sid)` → SIGTERM →
  grace → SIGKILL if alive → roll flag back on total failure.
- `close`: resolve group → `arm_close(pid)` → SIGTERM → grace → SIGKILL if
  alive → roll flag back on total failure.
- Failure statuses **propagate** (the swallowed-exit-code → green-checkmark
  lesson).

*(Slice 1 shipped `close` with no flag and both signatures carrying an unused
`now` param, since dropped. Slice 2's server slice adds `flags` +
`arm_close` to `close`.)*

### CLI + web

- `crr kick <pid>` / `crr close <pid>` handlers calling `ops.*`.
- Two new `/api/action` ops (`kick`, `close`) added to `ACTIONS`, same
  strict validation, calling the same `ops.*` — one gate, both surfaces
  (mirrors reopen/dismiss/remove).
- Dashboard buttons: **Kick** + **Close** on `live`/`ghost` cards
  (`PAGE_VERSION` bump). Kick/Close never shown on `crashed` cards (Reopen is).

### Shim repair loop (fish, bash, zsh — all three, together)

The `claude()` wrapper gains a loop around `command claude`, reading the flag
after each exit and branching on its state:

1. **Start:** clear any stale flag for this pid.
2. Run `command claude …`; capture exit code.
3. **Read the flag.** Then:
   - **relaunch**(sid) (kick): silently `command claude --resume <sid>`; loop.
   - **close** (close): run `claude-exit` (deregisters), then **`exit` the
     shell** — the tab/pane/ssh session closes. Terminal state; no loop.
   - **absent + nonzero** (crash): print `crr: claude exited unexpectedly
     (<code>). Resume this conversation? [Y/n]` and read the answer. **yes /
     timeout / no-tty → resume**; explicit **no → stop**. Loop on resume.
     (Timeout uses the shell's timed read where it has one — bash/zsh
     `read -t`; fish lacks a native timed read, so it falls back to a
     blocking read and relies on the no-tty→resume rule for the unattended
     case.)
   - **absent + exit 0** (you quit claude): stop; back to the prompt.
4. **Bounded to 2 resume attempts** per invocation — a session that keeps
   dying gives up in place (the shim analogue of the reviver's give-up guard)
   instead of spinning, then falls through to `claude-exit`. (The `close`
   branch is terminal and not subject to the cap.)

A small shim-facing helper backs this: `crr repair-check --pid` prints the
flag kind + sid (and `--clear` unlinks it), reusing the journal. Behaviour is
identical across the three shells; only syntax differs.

**Slice-2b hard requirements (from Slice-2a's final review — the wrapper MUST
honour all four):**

1. **Unknown kind → treat as absent.** `repair-check` is deliberately
   kind-agnostic; a stale Slice-1 bare-sid flag surfaces as an unrecognized
   kind. The wrapper's branch must fall through to offer/return, never act on
   an unknown kind.
2. **`relaunch` with no sid → treat as absent.** Never run `claude --resume`
   with an empty argument (guards the empty-sid edge; the producer doesn't
   emit one today, but the wrapper must not trust it).
3. **Parse the line yourself.** fish command substitution splits on newlines
   only, so `set flag (crr repair-check …)` yields the whole line in
   `$flag[1]`; split on space (`string split ' '` in fish; `read kind sid` in
   bash/zsh). Absent = empty output → empty list.
4. **No atomic read-and-clear.** `read` and `--clear` are two separate `crr`
   invocations; an op that re-arms between the wrapper's read and its clear is
   silently discarded. Design the loop as read-then-clear and accept the small
   window (or add a clear-on-read mode later).

## Slice-2b resolved design decisions (recorded autonomously per the handoff)

Decisions the spec left open, resolved before implementation; each picks the
sensible default aligned with DESIGN.md and the existing shim code:

1. **Terminal states all run `claude-exit`.** Clean exit, explicit "no" at the
   offer, the give-up cap, and the `close` branch all end with
   `crr claude-exit --pid <pid>` — exactly the states where the session is
   over and the card must not linger. Silent relaunch and offer-accepted
   resume skip it (the journal entry, with its sid, must survive into the
   next iteration).
2. **The ≤2-attempt cap guards the *crash* branch only.** The cap exists to
   stop an unattended auto-resume spin (timeout/no-tty → resume). A
   `relaunch` flag cannot spin — each one requires a fresh, deliberate
   `kick` — so flag-driven relaunches neither count toward nor are blocked
   by the cap, and a successful relaunch resets the crash counter (a kick is
   operator intervention, the thing the give-up guard waits for). The
   `close` branch is terminal and exempt, as the spec already states.
3. **Crash-resume target:** the wrapper tracks the current sid locally
   (injected sid on fresh launch, explicit sid on resume, sid from each
   consumed `relaunch` flag). Offer accepted with a known sid →
   `command claude --resume <sid>`. Sid unknown (e.g. `--continue` or a
   picker launch that stayed untracked) → journal via
   `crr claude-resume --pid <pid> --cwd <cwd>` (guessed from the newest
   transcript, matching the existing resume path) and run
   `command claude --continue` — after a crash, the crashed conversation is
   the newest in that cwd.
4. **TTY handling is uniform:** each shell first tests `test -t 0`; no tty →
   resume immediately, no prompt. With a tty: bash/zsh use `read -t 30`
   (timeout → resume), fish uses a blocking `read` (no native timed read —
   the no-tty guard covers the unattended case). Empty answer (Enter) →
   resume; only an explicit n/N/no answer declines. The 30-second timeout is
   a named variable at the top of each shim (`_CRR_OFFER_TIMEOUT`), not a
   magic number inline; it is shim-local because the dependency-free shims
   do not read crr config.
5. **The relaunch loop passes no user args:** resume iterations run exactly
   `command claude --resume <sid>` (or `--continue` per decision 3) — the
   original argv already did its job on the first run; replaying prompts or
   one-shot flags on a resume would be wrong.
6. **Flag reads are `read` then `clear`, immediately adjacent** (two `crr`
   calls, hard requirement 4): the wrapper clears the flag before acting on
   the parsed value, accepting the small re-arm window the spec documents.

## Contracts / versioning impact

- **No journal schema change** — the flag lives outside the journal.
- **No new sessions/diagnostics contract** — cards gain buttons, not fields.
- **`PAGE_VERSION`** bumps (new Kick/Close buttons in `page.html`).
- New CLI subcommands + two new `ACTIONS` entries.

## Testing strategy

**Slice 1 (server side) — SHIPPED.** `PsProcessController` builders, 1-state
`FlagStore`, `ops.kick`/`ops.close`, CLI + web + dashboard buttons; unit-
tested with fakes.

**Slice 2a (server-side flag) — fully unit-testable with fakes:**
- `FlagStore` 3-state: `arm_relaunch`/`arm_close`/`read`→(kind,sid)/`clear`,
  atomicity, stale isolation, kind round-trips.
- `ops.close` arms the close flag + rollback-on-signal-failure; `ops.kick`
  now calls `arm_relaunch`. Both still classifier-gated.

**Slice 2b (shim repair loop) — `test_shims.py`, gated per installed shell:**
- stale-flag cleared at start; **relaunch** flag → silent resume; **close**
  flag → `claude-exit` + shell `exit`; **absent + nonzero** → offer (drive
  `yes`/`no`/timeout/no-tty); **absent + exit 0** → no resume; 2-attempt cap.
- `crr repair-check` helper: prints kind+sid; `--clear` unlinks.
- Live verification on Linux/WSL (fish); macOS live run awaits hardware.

Every step is test-first (red → green), advisor before/after each slice,
`PAGE_VERSION` discipline, honest calibration.

## Delivery — three mergeable slices

1. **Server side (SHIPPED)**: port + `PsProcessController` + 1-state
   `FlagStore` + `ops.kick/close` + CLI + web endpoints + dashboard buttons.
2. **Slice 2a — server-side flag**: `FlagStore` → 3-state; `ops.close` arms
   the close flag (rollback on failed kill); `ops.kick` → `arm_relaunch`.
   Pure, fully unit-testable, one branch. Zero shell risk.
3. **Slice 2b — shim repair loop**: the shell code in all three shims
   consuming the 3-state flag (relaunch → resume, close → exit, absent →
   offer), the `crr repair-check` helper, `test_shims.py` coverage, live
   Linux/WSL verification.

The cutover (moving `cc-*` under crr's shim) is a **separate** operational
plan authored after #4 lands.

## Blockers B1/B2 — RESOLVED

Slice 1's final review surfaced two gaps; the 3-state flag protocol above
resolves both (see "The flag protocol (3-state)"): a `close` arms a distinct
`close` flag so the wrapper never resumes it (B1), and `close` ends the whole
terminal by having the wrapper run `claude-exit` then `exit` the shell —
graceful, card-clearing, no external shell-kill (B2). Retained here only as
the rationale trail.

## Acceptance criteria

- `crr kick <pid>` on a live/ghost session: claude's group dies, the relaunch
  flag is armed, and (under crr's shim) claude comes back on the same
  conversation; on a crashed session it is refused.
- `crr close <pid>` on a live/ghost session: claude's group dies, the close
  flag is armed, and (under crr's shim) the wrapper deregisters and exits the
  shell so the terminal closes and the card disappears; on a crashed session
  it is refused. Both ops roll their flag back if the kill fails.
- Repair loop: relaunch flag → silent resume; close flag → deregister + exit;
  crash (no flag, nonzero) → offer, resume on yes/timeout/no-tty, stay dead on
  explicit no; user-quit (clean exit) → never resumes; ≤2 resume attempts.
- Kill is always by process group of the claude child, never by cmdline.
- Implemented + unit-tested on Linux/WSL/macOS and fish/bash/zsh; live-run on
  Linux/WSL. No feature is Linux-only.
