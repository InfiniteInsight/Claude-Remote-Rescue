# Spec — Part B: detect a dropped Remote Control and reconnect it

Status: drafted 2026-08-07, awaiting review. Part A (always launch with
`--remote-control`) is a separate, smaller change.

## Problem

A session's Remote Control link to the phone can drop while claude keeps
running locally. The mobile list then shows it as **Disconnected**, and
there is no way to act on it remotely — which is the whole point of crr.
Today nothing notices, and the fix is manual: find the session, restart it.

## What is NOT possible (checked, so it isn't re-attempted)

**There is no local port to monitor.** Live `claude` processes own **zero
listening sockets** — Remote Control is an outbound connection to
Anthropic's cloud. Any design based on watching a port is a dead end.

## The signal that does exist

Claude Code writes markers into the transcript while the bridge is up:

```json
{"type":"bridge-session","sessionId":"93122659-…",
 "bridgeSessionId":"cse_019Wep…","lastSequenceNum":"8453"}
```

Measured across the 20 most recent real transcripts (`.claude-mem`
excluded):

| | |
| --- | --- |
| sessions with bridge markers | 18 |
| sessions with none (Remote Control never enabled) | 2 |
| median records between consecutive markers | 13–21 (per session) |
| **worst legitimate gap observed** | **67 records** |
| newest marker's distance from the tail, healthy sessions | 0–11 records |

So on a live, bridged session the newest marker sits within ~11 records of
the tail, and never more than 67 behind.

**Detection rule:** count records newer than the newest `bridge-session`
record. If that count exceeds a threshold while the session is LIVE, the
bridge has dropped but claude is still working.

Why count RECORDS and not elapsed time: an idle session writes nothing at
all, so a time-based rule would fire on every session you simply walked
away from. Counting records is self-normalising — the counter only grows
when claude is actually producing work, which is exactly the "dropped
mid-work" case worth acting on.

**Threshold:** `bridge_stale_records`, default **150** — a >2x margin over
the worst legitimate gap (67). Measured, not guessed; a session that
exceeds it has produced ~4 turns' worth of records with no bridge marker.

## The two cases that must not fire

1. **Remote Control was never enabled** — zero bridge markers in the whole
   transcript. You cannot drop what was never up. Such sessions are not
   monitored at all (2 of 20 here).
2. **Idle session** — no new records, so the counter does not grow. Falls
   out of the design rather than needing a special case.

## Design

### Detection (pure core: `crr/core/bridge.py`)

```
bridge_state(records_after_newest_marker: int,
             had_marker: bool,
             *, stale_after: int) -> "off" | "ok" | "dropped"
```

- `off` — no marker ever seen (never enabled): not monitored.
- `ok` — within the threshold.
- `dropped` — had a marker, and more than `stale_after` records since.

Pure and injectable, like `takeover.ready_to_take_over`. The adapter
counts on the existing backward transcript walk (the newest marker sits
within ~11 records on healthy sessions, so this early-exits cheaply and
only walks far on sessions that are actually stale).

### Surfacing (always) — a card badge

A `remote_control` field on the session card (`off` / `ok` / `dropped`),
rendered as a **"remote control dropped"** badge, in the same family as the
existing pressure badges. This is valuable on its own: even with auto-kick
off, the dashboard tells you which sessions have gone dark on the phone.

Card contract gains `remote_control` and `autokick` ->
`SESSIONS_CONTRACT_VERSION` bump.

### Acting (opt-out) — reconnect by kicking

When a session is `dropped`, crr restarts claude on the same conversation.
The relaunch carries `--remote-control` (Part A), so it comes back
connected.

**A kick mid-turn destroys the in-flight turn.** So the auto-kick reuses
`crr.core.takeover.ready_to_take_over`: it only fires when the transcript
is quiet for `takeover_idle_seconds` AND the tail is a completed assistant
turn. If the session is mid-turn it waits for the next pass — seconds to
minutes, nothing lost. This is the same safety property `adopt --takeover`
already ships, reused rather than reinvented.

Deliberate exits need no special handling: `exit`/`quit`/Ctrl-C leave no
process, so there is nothing to kick and the state is `crashed`, which this
path ignores by construction (it only acts on LIVE sessions).

### Where it runs

Inside the existing watchdog pass (`crr revive`, 30s timer) as a **separate
step with its own gate**, not folded into the revival logic — the reviver
acts on CRASHED sessions, and this acts on LIVE ones, which is a different
and more dangerous class of action. Keeping them separate keeps the
revival path's reasoning intact.

### Config

- `remote_control_watch` (bool, default **true**) — do the detection and
  show the badge.
- `remote_control_autokick` (bool, default **true**) — the GLOBAL hard
  switch. False means nothing is ever auto-kicked, whatever a session says.
- `bridge_stale_records` (int, default **150**) — the measured threshold.

### Two levels of auto-kick control: global switch, per-session opt-out

Auto-kick is controllable at both levels, and the global one is a **hard
switch** — not a default that a session can override:

| global | per-session | result |
| --- | --- | --- |
| OFF | (anything) | **never auto-kicked.** Per-session values are ignored but RETAINED, so flipping global back on restores them. |
| ON | unset | auto-kicked (per-session defaults to on) |
| ON | off | not auto-kicked — this one session is pinned out |
| ON | on | auto-kicked |

The asymmetry is deliberate. Global OFF is the panic switch: when the
feature is misbehaving you need one action that stops all of it, with no
per-session exception able to keep restarting things behind your back.
Global ON is permissive, because at that point you have opted in and the
interesting control is "all except this one session I'm babysitting".

Per-session state is keyed by **session id**, not pid — a pid is recycled
and would silently transfer your opt-out to an unrelated session.

The card carries an `autokick` field so the toggle renders its true current
state, including "off because global is off" (shown disabled, with the
reason, rather than a lying ON).

### Turning auto-kick off — from the phone, not just the machine

`config.toml` is only editable on the host. That is the wrong place for the
kill switch on the one behaviour in crr that restarts a live session by
itself: the moment you most want it off is when it is misfiring while you
are away from the machine.

So **both toggles live in the dashboard** — the global one in the Settings
modal alongside the excluded-directories editor, the per-session one on the
card itself — using the same mechanism that already ships:

- Stored in the dashboard-owned JSON in the state dir (the store behind
  `crr.core.exclusions` generalises to a small settings file — the web must
  never rewrite the user's hand-maintained TOML, since the stdlib has no
  TOML writer and a generated file would lose their comments).
- Read as: `config.toml` supplies the default, the dashboard-managed value
  overrides it when present. Same layering as the exclusion list.
- Served/written through the existing `/api/exclusions`-style endpoint
  pattern: host allowlist, JSON content-type gate, atomic write, validated
  type.
- The Settings row states plainly what it does and that turning it off
  keeps the badge — so the diagnosis stays even when the action stops.
- The per-session toggle is a card action (POST by session id, the existing
  `/api/sid-action` namespace), and renders disabled with a reason when the
  global switch is off rather than showing a state it cannot honour.

A CLI equivalent (`crr config --set remote_control_autokick=false`) is
explicitly NOT part of this: crr does not write `config.toml`, and adding a
second writer for it is out of scope. Editing the file by hand still works
and remains the machine-side path.

## Risks accepted

- **`bridge-session` is undocumented internal format** and may change. Like
  every other transcript field crr reads (prompts, models, titles), it must
  degrade to `off` — never raise, and never auto-kick on a parse failure.
- **A false positive costs a restart.** Bounded: the conversation is
  preserved (kick resumes the same sid), and the boundary-wait means no
  in-flight turn is lost. The kill switch is one config key.
- **The threshold is measured on one machine's 20 transcripts.** It is a
  named, injectable prior — not a constant buried in logic — so it can be
  raised without a code change if a false positive shows up.

## Non-goals

- Watching a port (none exists).
- A CLI writer for `config.toml` (crr does not write that file).
- Reconnecting without restarting claude — no local mechanism exists to
  re-establish the bridge in place.
- Acting on CRASHED or GHOST sessions (that is the reviver's job).

## Test plan

- Core `bridge_state`: below/at/above threshold; `had_marker=False` -> `off`
  regardless of count; boundary values.
- Adapter: counts records after the newest marker on a fake transcript;
  returns "no marker" honestly; degrades on unreadable/corrupt files.
- Card contract: new field, version bump, validator, fixtures.
- Watchdog: a `dropped` + boundary-ready LIVE session is kicked; a
  `dropped` but mid-turn session is NOT kicked this pass; an `off` session
  is never kicked; a CRASHED session is untouched by this path;
  `remote_control_autokick=false` shows the badge and kicks nothing.
- Settings toggle: the dashboard-managed value overrides the config default;
  absent means fall back to config; a bad stored value degrades to the
  config default rather than raising; turning it off leaves the badge.
- The four rows of the global/per-session truth table, including that
  per-session values SURVIVE a global off/on cycle.
- Per-session state keyed by sid: a recycled pid must not inherit another
  session's opt-out.
- The card toggle renders disabled (with the reason) when global is off,
  never a state it cannot honour.
