# Reviver hardening: stop reviving sessions that can never come back

**Date:** 2026-08-29
**Status:** approved (user-approved design, this doc is its record)

## Problem

A claude session that starts and dies (or wedges) without ever becoming a
healthy conversation is revived on every boot, forever. The strike
mechanism (`revive_strikes` / `zombie_strikes`) exists to stop exactly
this, but it never engages, because `_decide` resets strikes to zero
whenever the tmux session is *observed alive* — and a broken revival
routinely lingers alive (claude sitting at the trust-folder prompt in a
detached session nobody sees). Observed cycle: boot → revive (strike 1) →
stuck alive → watchdog pass resets to 0 → dies → next boot revives again.

Concrete instance (2026-08-29): five sessions journaled by failed
`claude rc` invocations — conversations that never existed — were revived
into tmux sessions + tabs on every WSL boot. Liveness is the wrong health
signal.

## Design

Two independent layers, plus visibility.

### Layer 1 — resumability pre-flight

Before spawning a revival (both the active-journal loop and the archive
loop in `reviver.revive_crashed`), probe whether the conversation's
transcript exists (`~/.claude/projects/<encoded-cwd>/<sid>.jsonl`).

- **Confirmed absent** → the conversation cannot possibly resume: archive
  the entry terminally with new reason `unresumable` (journal loop:
  archive + delist; archive loop: rewrite the record's reason). No spawn,
  no tab, no trust prompt.
- **Unknown** (probe error, unreadable dir) → never blocks revival (F16
  tri-state discipline: an unconfirmed absence must not destroy a
  possibly-live conversation's revival).
- `unresumable` joins the terminal-reasons skip list in the archive loop.

New port in `crr/core/ports.py`:

```python
class TranscriptProbe(NamedTuple):
    exists: bool | None   # None = could not determine
    mtime: float | None   # None when absent or unknown

class TranscriptSource(Protocol):
    def probe(self, session_id: str) -> TranscriptProbe: ...
```

Adapter: thin wrapper over the existing
`crr.adapters.transcript_source.find_transcript` + `Path.stat`, living in
`crr/adapters/transcript_source.py`. Any exception → `(None, None)`.
Wired in `crr.cli` at every `revive_crashed` call site (layering:
cli → adapters → core preserved; core sees only the port).

### Layer 2 — activity-keyed strike reset

Each revival stamps the transcript's current mtime onto the journal entry
as `revived_tx_mtime` (float | null). When a later pass observes the
session alive, strikes reset **only if** one of:

- the entry carries no `revived_tx_mtime` stamp (legacy entries → old
  eager-reset behavior, no migration needed), or
- the transcript's mtime has advanced past the stamp (real conversation
  activity since the revival), or
- the session is currently **attached** (a human is in it).

Otherwise a new `hold` action leaves the entry untouched: a
stuck-alive-but-frozen session keeps its strikes and reaches give-up.
`revive_crashed` gains optional `transcripts: TranscriptSource | None`
and `attached: set[str] | None` params; when absent (older callers,
pure-core tests) behavior is exactly today's.

`attached` is supplied from `tmux.attached_sessions()` at the call sites
that have it; `None`/unknown degrades to "not attached" — which only
withholds a reset, never destroys anything.

### Config

`zombie_strikes` default 3 → **5** (user decision: more runway so a
healthy-but-idle revived session that nobody touches across several
reboots is not archived prematurely). Config-defaults version bump per
convention. Sessions that strike out are archived as `gave-up`
(recoverable — not deleted).

### Visibility (user requirement)

Strike escalation must be visible in the CLI, not silent:

- **`crr rescue-check`** (the boot-time restore that opens tabs): each
  restored session's printed line gains a strike suffix when its entry
  carries strikes, e.g.
  `restarted crr-<sid> (opened in a new tab) · strike 2/5 — stops reviving at 5`.
  Strikes are read from the journal entry (the silent revive pass that
  runs earlier in rescue-check has already stamped them).
- **`crr revive`**: `RevivalOutcome` gains `strike_counts: dict[int, int]`
  (revived pid → new strike count; defaulted `{}` so existing
  constructions stand). Output lists revived pids with `strike N/5` and
  reports any `unresumable` archivals.
- **`crr status`**: entries with `revive_strikes > 0` show
  `strike N/5` on their line.
- `RevivalOutcome` also gains `unresumable: list[int]` (defaulted).

## Error handling

- All probes tri-state; only *confirmed* absence gates revival.
- Stamp comparison tolerates missing/None mtimes: no current mtime →
  no activity evidence → hold (safe: withholds reset only).
- No dashboard/page changes (PAGE_VERSION untouched).

## Testing (TDD, red first)

`tests/test_reviver.py` with a fake `TranscriptSource`:

1. Confirmed-absent transcript → archived `unresumable`, no spawn (both
   journal and archive loops).
2. Unknown probe → revives exactly as today.
3. Alive + mtime unchanged since stamp → strikes held (no reset).
4. Held strikes reach `max_strikes` → gave-up.
5. Alive + mtime advanced → reset to 0.
6. Alive + attached → reset to 0.
7. Legacy entry without stamp → eager reset (compat).
8. Revival stamps `revived_tx_mtime`; `strike_counts` reports pid → count.
9. `zombie_strikes` default is 5.
10. CLI: rescue-check line and `crr revive` output include strike text;
    status shows `strike N/5`.
