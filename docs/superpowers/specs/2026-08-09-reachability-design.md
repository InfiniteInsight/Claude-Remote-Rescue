# Reachability — replace the dropped-bridge detector, and stop the dashboard lying about state

**Status:** design · 2026-08-09
**Supersedes:** `2026-08-07-remote-control-watchdog-design.md` (Part B's detector)
**Issues:** closes the detector half of #33's motivation; new work

---

## Why

The Part B watchdog detects a dropped Remote Control bridge by counting
transcript records since the newest `bridge-session` marker. Measured on
this machine's own corpus (93 transcripts, 3,659 continuously-active
windows), reaching the 150-record threshold takes a **median of 8 minutes
of active work** — and on an **idle** session it never happens at all,
because an idle session writes no records.

That inverts the point of the feature. The builder's words:

> I'm seeing a lot of disconnected sessions that will never auto kick and
> just be ready when I need them to be most, when I'm mobile.

The original design defended record-counting on the grounds that a
time-based rule "would fire on every session anyone ever walked away
from" — treating an idle disconnected session as a false positive. It is
not a false positive. It is the case the feature exists for. A kick costs
a restart that resumes the same conversation at a clean boundary; that is
cheap. The design optimised against the wrong error.

Two further defects, found while investigating, share the theme *the
dashboard asserts things that are not true*:

- A session revived into tmux after a reboot displays as `crashed`
  forever, while `crr revive` reports it "already running". Two
  subsystems disagree and the card shows the wrong one.
- The shell restore prompt races the watchdog and loses, so after a
  reboot the builder is never offered the restore they expect.

---

## The signal

`~/.claude/sessions/<pid>.json`, written by Claude Code itself.

```json
{
  "pid": 2067,
  "sessionId": "51020c8a-…",
  "status": "idle",
  "waitingFor": "permission prompt",
  "bridgeSessionId": "session_013C…",
  "updatedAt": 1786…, "statusUpdatedAt": 1786…
}
```

### Evidence it is authoritative

Read from the shipped bundle (`~/.local/share/claude/versions/2.1.220`),
not inferred:

- The bridge session is one module-level variable `Rwo`, with exactly one
  setter:
  `function Jks(e){ let t=Rwo; Rwo=e; …; ORc(r ?? null); … }`
- `ORc` is the only writer of the field:
  `async function ORc(e){ await iHt({bridgeSessionId:e}) }`, where `iHt`
  is `updatePidFile` (named by its own log line
  `[concurrentSessions] updatePidFile failed`).
- Teardown calls the setter with null:
  `… teardown({skipArchive, reason}), d.current=null, Jks(null)` — and it
  carries a `reason`, so error-driven teardowns take the same path.
- The app's own user-facing copy defines "connected" as this variable:
  `I = !jw() || !Mx()` → *"Remote Control is not connected"*, where
  `function jw(){ return Rwo }`.

So a bridge coming up or going down and the file being written are the
same event.

### The one thing it is NOT

The `/rc` footer indicator does **not** render from `Rwo`. It renders from
React app state:

```js
function T1S(m1S){ return m1S.replBridgeEnabled && m1S.replBridgeError === void 0 }
```

Both are updated in the same connect/teardown handlers, so they normally
agree — but the **error** path sets `replBridgeError` + `replBridgeEnabled:
false` without routing through `Jks`. Enumerating all 8 `iHt` call sites
confirms **no error field is ever persisted**: the writers are
`sessionId`, `cwd`, `{name, nameSource}`, `{bridgeSessionId}`,
`{parkedJobId}`, and a generic `{…, updatedAt, statusUpdatedAt}` for
`status`/`waitingFor`.

**Consequence, stated plainly.** Error states split:

| case | file | detector |
|---|---|---|
| bridge never established (repeated init failure — the common error) | `bridgeSessionId` never written → null | detects, kicks ✅ |
| bridge established, then errors **without** teardown | stale session id | **misses it** ❌ |

The miss is silent-but-safe: a kick that does not happen, never a wrong
kick, and manual Kick always works. It is made measurable rather than
theoretical by the transition counter below.

---

## Design

### Phase 0 — stop calling revived sessions "crashed"

Independent of everything else, and the most urgent because it is wrong
right now on 16 cards.

**Corrected 2026-08-09 during planning.** The first draft of this phase
put the new state inside `classify()`. That is wrong and would have broken
two ops on their only valid input: `ops.detmux` and `ops.untmux` both
guard with `if state != CRASHED: refuse (… re-homes revived sessions
only)`, and a tmux-parked session is precisely what they re-home.
`ops.dismiss` guards the same way, and `reviver`'s crashed-entry loop
would skip parked entries earlier than today, changing the "already
running N" accounting. Ten call sites, six of them gating destructive
operations.

`classify()` is the **operational** classifier — it answers *may I act on
this pid*, and `crashed` is the right answer for a parked session, which
is exactly why `detmux` keys on it. It is left untouched.

The lie lives in the card's `state`, which is a **display projection**.
That projection is computed in `status.py` and nowhere else:

```python
state = classify(entry, boot_identity, probe)
if state == CRASHED and tmux_alive(entry.get("tmux_session")):
    state = PARKED          # display only; ops still see CRASHED
```

A journal entry whose `tmux_session` is **confirmed alive** is not, to a
reader, crashed. The card gains a fourth state:

```
live     shell + claude running with a controlling terminal
parked   running inside a live tmux session, no terminal attached  ← new
ghost    shell alive, no controlling terminal
crashed  process gone, or the host rebooted and nothing was revived
```

`parked` is what a reviver-restored session actually is. The classifier
must consult tmux liveness the way `crr revive` already does, and must
honour F16's tri-state explicitly:

| tmux liveness | entry has `tmux_session` | result |
|---|---|---|
| session confirmed **alive** | yes | `parked` |
| session confirmed **absent** | yes | classify as before (boot/pid evidence → usually `crashed`) |
| **unknown** (`list_sessions()` → None) | yes | classify as before, and report the uncertainty on stderr as `crr rescued` already does — an unconfirmed tmux state may neither promote to `parked` nor be presented as settled |
| any | no | classify as before, unchanged |

This keeps the rule one-directional: tmux liveness can only ever *rescue*
an entry from a wrong `crashed`, never push one into it.

Card shows `restored — attach or reopen`. `contracts.STATES` gains
`parked` (it validates the card, not `classify`'s return);
`SESSIONS_CONTRACT_VERSION` bumps. `classifier.py` is unchanged, so
`ops.py`, `reviver.py` and `cli._kick_dropped_bridges` need no edits and
no re-testing of their guards.

The tmux-liveness lookup is the set `assemble_sessions` can be given once
per poll — the same injection shape as `tail_facts` — so core still does
no I/O.

### Phase 1 — the reachability detector

**New adapter** `crr/adapters/session_state.py`. One directory scan per
poll, not per card:

```
read_all(home) -> {session_id: SessionState(pid, bridge_session_id,
                                            status, waiting_for, updated_at)}
```

Resolution per session. Every step can only fall to `unknown`, never to a
positive claim:

| step | on failure |
|---|---|
| newest state file for this `sessionId` (by mtime) | none → `unknown` |
| its `pid` ∈ the claude group crr already tracks for that session | mismatch → `unknown` |
| the file carries a `bridgeSessionId` key at all | absent → `unknown` (older schema) |
| `bridgeSessionId` truthy? | → `reachable` / `unreachable` |

The pid check is load-bearing: this machine has 133 state files, of which
**117 belong to dead pids and 2 to recycled pids now owned by unrelated
processes**. One session had three files, two with "alive" pids. Liveness
alone gives a confident wrong answer.

**New pure core** `crr/core/reachability.py` — classification only, no
I/O, mirroring `bridge.py`'s shape:

```python
def reachability(bridge_session_id: str | None, *, pid_matched: bool,
                 field_present: bool) -> str   # unknown|reachable|unreachable
def may_kick(status: str | None) -> tuple[bool, str]
```

**Layering:** the adapter reads the filesystem, core classifies, `cli`
builds the map once per poll and injects it into `assemble_sessions` —
the same shape as `tail_facts` today.

### Phase 2 — the kick rule

Replaces the record threshold and the transcript-boundary check.

| `status` | kick? | corroboration | why |
|---|---|---|---|
| `busy` | no | — | computing — this is where work is lost |
| `shell` | no | — | a command is running |
| `idle` | **yes** | **AND `takeover.ready_to_take_over`** | finished a turn; a second, independent signal guards against a stale file |
| `waiting` | **yes** | none — deliberately skipped | blocked on the user — see below |
| absent / unrecognised | no | — | unknown is not a licence to act |

The `idle` row keeps the transcript-boundary corroboration the builder
asked for ("two independent sources must agree before a live process is
signalled"). It costs nothing — both reads already happen — and it means a
single stale state file cannot license a mid-turn kill on its own.

`waiting` is the deadlock-breaker and the reason this is not a flat AND of
two signals. A session blocked on a permission prompt never reaches an
`assistant-end` tail, so the old `ready_to_take_over` guard would veto
exactly the case that most needs unsticking: blocked on a question the
builder cannot answer, because the phone is disconnected. Both values are
present on disk right now — `waitingFor: "permission prompt"` and
`waitingFor: "input needed"`, each with `bridgeSessionId: null`.

**Work-loss, honestly:**

- `waiting: input needed` — nothing lost; Claude asked and stopped.
- `waiting: permission prompt` — the **pending tool call** is lost; it
  will not run. Conversation history survives via `--resume`.
- `busy` / `shell` — real work dies. Hard blocks.
- **Any state — the process GROUP.** `ops.kick` sends `SIGTERM` to
  `killpg(pgid)`, so backgrounded bash jobs, a dev server launched from the
  session, and in-flight subagents die with claude regardless of which
  status permitted the kick. Neither guard can see them: `status` describes
  claude, the transcript boundary describes the conversation, and neither
  describes the process tree. Accepted deliberately — the alternative is a
  session that stays unreachable forever — and bounded by the cooldown, the
  attempt cap, and the per-session and global switches. (Added 2026-08-10:
  the first draft of this table counted only the un-started tool call.)

Retained unchanged from Part B: the cooldown
(`bridge_kick_cooldown_seconds`), the attempt cap
(`bridge_kick_max_attempts`), reset-only-on-observed-reachable, the
one-kick-per-sid-per-sweep guard, fail-closed on a degraded settings or
kick-history store, and the `remote_control_watch` /
`remote_control_autokick` toggles.

Latency becomes **one sweep (≤30s)** from the bridge dropping, regardless
of whether the session is doing anything.

### Phase 3 — the card

`remote_control` keeps its name; its enum becomes:

| value | badge |
|---|---|
| `reachable` | *(nothing — the common case)* |
| `unreachable` | `phone: not connected` |
| `unknown` | `phone: unknown` |

`off`, `ok` and `dropped` are gone. There is no `unmanaged` state: a
session in the card list is managed by definition, and Discoverable is
where unmanaged ones live — the distinction is already carried by which
list you are looking at.

New card field `waiting_for` (string, `""` when absent) renders as
`waiting on you` beside the badge, so the builder knows *why* a session is
stuck before crr restarts it.

Header counter: `14 reachable · 2 not connected`, answering "am I good to
leave the desk?" without reading 16 cards. A `not reachable` filter chip
joins the existing crashed filter.

### Phase 4 — restore banner, replacing the shell prompt

The shell prompt cannot win its race. Measured on this machine:

| | |
|---|---|
| 14:09:26 | boot |
| 14:09:45 | shells start; `rescue-check` runs, finds tmux empty, correctly stays silent |
| 15:25:44 | systemd `--user` starts (WSL starts it lazily, 76 min later) |
| 15:26:37 | watchdog revives 13 into tmux |
| — | no new interactive shell afterward → the offer never returns |

Replace it with a dashboard banner: *"13 conversations restored after the
last reboot — open them in tabs?"* with **Open in tabs** and **Dismiss**.
No race (the dashboard re-polls), no 15-second timeout, and it works from
the phone — which is where the builder is when it matters.

- New `GET /api/rescued` → `{contract, boot_id, rows:[{session_id, sid8,
  cwd, tmux_session}], dismissed}`, contracted and validated like the
  five payloads #36 versioned.
- `POST /api/rescued {op:"dismiss"}` writes the existing per-boot marker
  (`rescue-prompted-<boot_id>`), reusing `rescue.claim_prompt`'s atomic
  semantics rather than inventing new state.
- `rescue-check` and its shim call site are **removed**. This requires the
  builder to regenerate shims (`crr shim fish` etc.); the release note
  must say so, because a stale shim keeps calling a command that no
  longer exists.
- `crr rescued` stays as the CLI read path.

### Phase 5 — the transition counter

The known gap (established-then-errored without teardown) must be
measurable, not theoretical. The watchdog records each observed
`reachable → unreachable` transition as a counter in the existing
`KickHistoryStore` (already versioned, already degrades honestly):

```json
{"v":1, "observed_transitions": 7, "last_transition_at": 1786…, "sessions": {…}}
```

Surfaced by `crr doctor`. If it stays at zero for a week while the builder
is seeing dead `/rc` indicators, the assumption is disproven and we have a
concrete instance to work from instead of speculation.

---

## What gets deleted

- `crr/core/bridge.py` — record counting, entirely.
- `bridge_stale_records`, `bridge_scan_lines` config keys.
- `bridge_seen` / `bridge_since` from `read_tail_facts`, and the
  bridge-marker branch of the backward walk (a per-poll cost on every
  card, now bought for nothing).
- `contracts.REMOTE_CONTROL_STATES`' `off` / `ok` / `dropped` members.
- `tests/test_bridge.py`; the bridge-marker tests in
  `test_transcript_source.py`.

`takeover.ready_to_take_over` is **kept** — `crr adopt --takeover` still
uses it. Only the watchdog stops consulting it.

---

## Contracts and config

| constant | change |
|---|---|
| `SESSIONS_CONTRACT_VERSION` | 10 → 11 (`parked` state, `remote_control` enum replaced, `waiting_for` added) |
| `RESCUED_CONTRACT_VERSION` | new, 1 |
| `CONFIG_DEFAULTS_VERSION` | 14 → 15 (two keys removed) |
| `PAGE_VERSION` | one bump per phase that changes the page; floor moves as other work ships (43 at time of writing) |

Per `tests/test_version_ledger.py`, every bump needs a ledger entry.

---

## Testing

**Pure core** — `reachability()` over its truth table including every
`unknown` route; `may_kick()` over all five status values plus `None`.

**Adapter** — newest-file-wins; a dead pid is ignored; a **recycled** pid
is ignored (the real hazard: build two files for one sid, one with a live
non-claude pid); an absent `bridgeSessionId` key reads `unknown`; a
corrupt file degrades without raising.

**Watchdog** — `waiting` is kicked even though its transcript tail is
mid-turn (the deadlock-breaker; this must fail if the old boundary guard
is reintroduced); `busy` and `shell` are never kicked; `unknown` is never
kicked; cooldown, cap and fail-closed behaviour survive the rewrite
unchanged.

**Contracts** — every payload validates; `parked` cards validate;
the ledger guard passes.

**Live verification on hardware**, with the same discipline #45 used:
stage a session, confirm the state file transitions, confirm a kick fires
within one sweep of a real disconnect, and confirm no other session is
touched. State plainly in the report which paths were exercised against a
real process and which only with fakes.

---

## Sequencing

Phase 0 is independent and should land first as its own branch — it is a
live incorrectness on 16 cards today and needs none of the rest. Phases
1–3 are one unit (the detector is useless without the card, and the card
cannot render states the detector does not produce). Phases 4 and 5 are
each independently shippable afterwards.

## Risks

| risk | mitigation |
|---|---|
| `~/.claude/sessions/*.json` is undocumented internal state; the schema can change without warning | every read degrades to `unknown`, never to a positive claim; the `field_present` check catches a renamed field rather than reading it as null |
| Error-path losses are invisible (above) | silent-but-safe by construction; transition counter makes it measurable |
| A stale `status` licences a kick during work | `status` is written on transition, and the pid check discards dead writers; `busy`/`shell` are hard blocks; worst case is one lost turn on a session that was already unreachable |
| Removing `rescue-check` breaks stale shims | release note; the shim already guards with `test -x`, so a missing binary is silent, but a *present* binary with a removed subcommand would error — the shim must tolerate a non-zero exit |
