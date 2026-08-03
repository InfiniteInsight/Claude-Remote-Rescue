# Spec — `crr adopt --takeover` (safe adoption of a still-live session)

Status: approved (design agreed with user 2026-08-03; advisor-reviewed;
empirically grounded on HedyLamarr).

## Problem

`crr discover --adopt SID` writes a synthetic, CRASHED journal entry for an
untracked transcript so it shows up as a recoverable card. The watchdog then
revives it as `claude --resume <sid>`. If a **real claude is still running**
on that same conversation elsewhere, adoption spawns a *second* `--resume` on
one transcript — two claudes appending to one JSONL. Today this is only
disclosed in copy, never prevented.

`--takeover` makes adoption safe for a live session: identify the running
claude, wait until it is between turns (so no in-flight work is lost), stop it
cleanly (without its shim repair loop respawning it), then adopt — so the
watchdog's revive is a genuine *takeover*, the sole claude on the conversation.

## Scope (v1)

- **CLI only.** New `crr adopt SID [--takeover] [--wait SECONDS]`. Plain
  `crr adopt SID` is the existing adopt path (`_adopt`); `--takeover` adds the
  live-process handling. `crr discover --adopt SID` stays unchanged as the
  older alias. **No dashboard surface, no page change, no `PAGE_VERSION`
  bump** — a destructive, default-off op behind a web button is the wrong
  first surface (advisor). Mention CLI-only in the final summary.
- Default-off and destructive: it SIGTERMs a live process. It only ever runs
  when `--takeover` is passed.

## Empirical grounding (HedyLamarr, 2026-08-03)

1. **claude does not hold the transcript fd open** — a `/proc/*/fd` scan for
   the live transcript found no holder. Writer identity is NOT resolvable by
   open file; it appends and closes.
2. **A shim/watchdog-launched claude carries `claude --resume <sid>` on
   argv** (verified via `ps`: `796194 916 796194 claude --resume 93122659-…`).
   The sid is positively identifiable there. A *freshly*-started `claude`
   (no `--resume`) generates its sid internally and does NOT carry it on argv
   — such a session cannot be located by sid (documented limitation → refuse
   branch, not a blocker).
3. **The tail record kind is the real safety signal, not mtime alone.** A live
   transcript's last record can be `type:"user"` carrying `toolUseResult`
   (a mid-turn tool result — claude will continue), which is NOT idle. mtime
   can also go quiet mid-turn during a long non-streaming completion. So
   "safe to take over" requires the tail to be a clean turn boundary
   (assistant `end_turn`, or a user prompt awaiting a reply), with mtime-quiet
   only as the cheap pre-filter.

## Design

### The discriminating gate

*Can crr positively identify the live process for this sid without guessing?*
- **Yes** → idle-wait → arm close flag on the wrapper → kill → adopt.
- **No** → refuse with the real next step: "no live `claude --resume <sid>`
  found; adopt without `--takeover`, or exit it in its own terminal first."
  (This is the honest home for the fresh-session limitation.)

### The sid-scoped argv match is NOT the "never kill by cmdline" violation

`process_probe._is_claude_argv0` warns against a *selector that's too broad*
("any process whose argv0 starts with claude"). Matching `--resume <specific
UUID>` is a different specificity class — one conversation, one match. Two
guards keep it honest:
- **Exclusion:** the target must be untracked (not crr-owned). The watchdog's
  own revivals also run `claude --resume <sid>`; adoption already only accepts
  discoverable (untracked) sids, and takeover **re-checks the sid is still
  untracked immediately before the kill** (close the resolve→kill window).
- **Kill by pgid, not by pattern.** The resolver returns the process's real
  `(pid, ppid, pgid)`; the kill uses the existing `terminate_group(pgid)`.

### The shim-repair-loop hazard (load-bearing)

The target may run under a shim-loaded shell whose repair loop resumes claude
on exit (realistic: was journaled+live, journal file lost/corrupted → sid
becomes discoverable while the repair loop is intact). Killing claude → the
shim respawns it → shim's claude *and* the watchdog's adopted claude on one
transcript = the exact corruption we're preventing.

Mitigation mirrors `_reopen_ghost`: **`flags.arm_close(ppid)` on the wrapper
(the resolved parent pid) BEFORE the kill.** The flag store is pid-keyed and
independent of the journal, so it works for an untracked session. If the
parent is not a shim wrapper, the flag is harmless (nothing consumes it; the
same profile as any armed-then-unused flag). Rollback rule (from
`_reopen_ghost`): if no kill lands, `flags.clear(ppid)` and fail with nothing
touched — a flag must survive only when a kill actually landed.

### Ordering (each step load-bearing)

1. **Resolve** the live process for `sid` → `(pid, ppid, pgid)` or None.
   None → refuse (fresh-session / not-running home).
2. **Idle-wait, refuse-fast** (cli-owned poll loop). Each poll samples the
   takeover signal (`read_takeover_signal` — tail kind first, then mtime; see
   below) and computes `seconds_idle = now - mtime`:
   - `seconds_idle < idle_window` → the transcript is still being written
     (claude is working) → keep polling. The **max-wait timeout bounds ONLY
     this wait-for-quiet phase** (continuous streaming): exceed it → refuse,
     no kill: `"still actively writing after Ns; not taking over"`.
   - `seconds_idle >= idle_window` → the transcript has gone quiet, so decide
     NOW (do not keep waiting — an idle process won't advance its own tail):
     - tail is `"assistant-end"` → **ready**, proceed to the kill.
     - tail is anything else → **refuse immediately** (fast, not after the
       full timeout): `"idle but parked at <tail_kind> — not a safe boundary
       to take over; finish or exit it manually"`. (~25% of real transcripts
       end this way — terminal killed mid-tool-call — so refusing fast here is
       the common, important case.)

   Every non-`assistant-end` branch is a REFUSAL, never a kill — refuse-fast
   cannot mis-kill; it only makes the safe refusal quicker.

   `idle_window` (`takeover_idle_seconds`) now *triggers a decision* rather
   than merely gating, so it must exceed the longest expected no-write gap
   during ACTIVE generation (extended thinking / a slow non-streaming API
   turn) — otherwise a long API gap reads as "parked" and false-refuses (a
   safe but unnecessary refusal). Hence the 20.0 default, not 12.0.
3. **Re-check** the sid is still untracked (exclusion guard against a
   resolve→kill race). Still tracked now → refuse.
4. **arm_close(ppid)**, then **terminate_group(pgid)** (SIGTERM/grace/SIGKILL,
   the existing machinery). No kill lands → `flags.clear(ppid)`, fail
   untouched.
5. **Adopt** via the existing `_adopt` (synthetic `adopted_pid`, CRASHED
   entry). The watchdog revives it as the sole `claude --resume <sid>`.

### Layering

- **core (pure):**
  - `crr/core/takeover.py`: `ready_to_take_over(seconds_idle, tail_kind, *,
    idle_window) -> bool` — pure predicate: `seconds_idle >= idle_window AND
    tail_kind == "assistant-end"`. No I/O, no sleep, no clock.
  - `crr/core/transcript.py`: `turn_boundary(record) -> str` — classify a
    single JSONL record: `"assistant-end"` (assistant message with
    `stop_reason == "end_turn"`, model not `<synthetic>`), `"user-prompt"` (a
    real user prompt, no `toolUseResult`), `"mid-turn"` (assistant with any
    other `stop_reason` — empirically `"tool_use"` even for text/thinking
    blocks — or a user `toolUseResult`), `"other"` (non-turn records:
    permission-mode, pr-link, bridge-session, `<synthetic>` assistant records,
    …). The tail-kind fed to the predicate is the kind of the newest
    turn-bearing record (skip `"other"`).
  - **`<synthetic>` assistant records classify as `"other"` (transparent),**
    matching `extract_model`/`_assistant_text`. `read_takeover_signal` scans
    backward for the newest non-`"other"` record, so an API-error/interrupt
    synthetic at the tail is skipped and the prior REAL turn decides. If that
    prior turn was an assistant `end_turn`, the session is genuinely parked at
    a clean, stable boundary → adoptable. Safety is NOT carried by the tail
    kind here: it comes from the separate active-session guard (`seconds_idle
    < idle_window` on a recent mtime → never ready), so transparency costs no
    safety.
  - **Only `"assistant-end"` is a safe boundary** (via the newest non-`other`
    tail record). Claude Code always emits an
    assistant turn after a user prompt, so on a *live* session a tail of
    `"user-prompt"` means the response is still pending (about to stream, or
    mid non-streaming API call) — unsafe. `"mid-turn"` is unsafe by
    definition. An assistant `end_turn` at the tail is precisely the common
    "finished, awaiting the user" idle state — safe. Rare non-`end_turn`
    terminals (`max_tokens`/`stop_sequence`) are conservatively treated as
    not-a-boundary: refusing (never mis-killing) is the safe failure.
- **adapters (I/O):**
  - `ProcessController.find_resume_process(session_id) -> ResumeProcess | None`
    (new port method) — one `ps` snapshot, match `claude --resume <sid>` on
    argv, return `(pid, ppid, pgid)`. `ResumeProcess` is a core NamedTuple.
  - `transcript_source.read_takeover_signal(session_id) -> {mtime, tail_kind}`
    — a bounded backward read for the newest turn-bearing record's
    `turn_boundary` (skipping `"other"`), THEN a stat for mtime. **Tail-first,
    stat-second** is deliberate: a concurrent append between the two reads
    then yields an mtime newer than the tail we read → `seconds_idle` small →
    not ready → keep waiting (the safe direction). Stat-first could pair a
    stale-quiet mtime with a tail that just changed. Absent transcript →
    honest empties (`mtime=0.0`, `tail_kind=""`).
- **cli (composition + orchestration):** the poll loop (real `time.sleep`),
  the flag arming, the kill, the adopt, all under `mutation_lock`. Wall-clock
  I/O lives here, never in core.

### Config (bump `CONFIG_DEFAULTS_VERSION`)

- `takeover_idle_seconds` (default 20.0): the quiet window that means "not
  actively streaming". Crossing it triggers the decision (ready vs
  refuse-parked), so it must exceed the longest expected no-write gap during
  active generation — 20.0, not 12.0 (see the wait-loop rationale).
- `takeover_max_wait_seconds` (default 180.0): bounds the wait-for-quiet phase;
  refuse (never kill) after this.
- `takeover_poll_seconds` (default 2.0): poll cadence for the wait loop.

## Tests (TDD, red first)

- **core `takeover.ready_to_take_over`:** below/at/above idle_window;
  assistant-end → ready once idle; user-prompt/mid-turn/other → never ready
  regardless of idle.
- **core `transcript.turn_boundary`:** real-shaped records — assistant
  end_turn, assistant tool_use, user prompt, user toolUseResult, and the
  non-turn `type`s (permission-mode/pr-link/bridge-session) → "other".
- **adapter `find_resume_process`:** fake `ps` rows — matches the sid,
  returns (pid,ppid,pgid); ignores a different sid; ignores a non-resume
  claude; None when absent.
- **adapter `read_takeover_signal`:** fake transcript → mtime + newest
  turn-bearing tail_kind; a `<synthetic>` (or other non-turn) tail is skipped
  to the prior real turn; absent → empties.
- **cli takeover happy path** (injected controller/flags/clock/sleep): order
  is resolve → wait-until-ready → arm_close(ppid) → terminate(pgid) → adopt.
- **cli refusals:** no process resolved → refuse, no kill, no flag;
  quiet-but-parked (tail not assistant-end) → refuse FAST (before max_wait),
  no kill, no flag; still-writing past max_wait → refuse, no kill, no flag;
  sid became tracked before kill → refuse; no kill lands → flag cleared, fail
  untouched.
- **cli wait loop timing** (injected clock/sleep): a session that streams then
  stops at `assistant-end` becomes ready without hitting max_wait; a session
  quiet-with-mid-turn-tail refuses at ~idle_window, NOT at max_wait.
- **Layering** `lint-imports` KEPT throughout.

## Non-goals (v1)

- Dashboard/web exposure (deferred; would extend `SID_ACTIONS`).
- Locating fresh (non-`--resume`) sessions by sid (impossible from argv;
  refuse branch).
- Cross-host takeover (single box only).
