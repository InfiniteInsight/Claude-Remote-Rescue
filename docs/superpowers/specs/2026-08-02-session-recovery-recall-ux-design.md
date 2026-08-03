# Session-recovery & recall UX

**Date:** 2026-08-02
**Status:** approved design, pre-implementation
**Base:** current HEAD `f277e80` (PR #32). Re-verify HEAD hasn't moved before merging each slice.

## Goal

Make crr honest and useful about *which session is which* and *what a session
knows*, and let you undo a de-tmux. Six features, one theme — you should
never (a) be unable to tell the latest session from an older one, (b) lose
access to conversation detail that's still on disk, or (c) be stuck after a
de-tmux.

Driven by two observed failures:
- A recently-active session (`6e262205`, which did real work Jul-29→Aug-2)
  was **invisible** to crr (never journaled), and the tracked session's
  recency signal was **~2 days stale** — so the latest session could not be
  identified from the dashboard.
- Long conversations compact during use; detail leaves the active context
  (though it stays on disk), with no way to recall it or to see it coming.

## Global constraints

- **One-way layering (CI-enforced):** `crr.cli → crr.adapters → crr.core`. Ports in `crr/core/ports.py`; adapters selected in `crr.cli`. `.venv/bin/lint-imports` must print `KEPT`.
- **Zero runtime dependencies** (stdlib only).
- **TDD**, test-first, watched-to-fail.
- **Versioned contracts:** any card/payload change bumps `SESSIONS_CONTRACT_VERSION` (+ validator + test). Any `page.html` change bumps `PAGE_VERSION`; keep the `node --check` gate green.
- **Honest calibration:** estimates are labeled estimates; model context windows are documented **priors** (audit P5), conservative when unknown.
- **Untrusted transcript fields** (last_prompt, recalled text, cwd) reach the DOM via `textContent` only.
- **Never dump wholesale:** `recall` is query-scoped and capped — it is grep-for-your-own-history, not a transcript dump.
- **Merge:** local-CI-green (`pytest` + `lint-imports` + `node --check`); feature branch → merge to main → push → `gh pr create` (Actions billing-blocked). Re-verify HEAD before each merge.

## Shared building block

`crr/adapters/transcript_source.read_tail_facts` already does ONE backward
read per card returning `{last_prompt, model}`. Extend it — in the same pass,
no extra I/O — to also return:
- `last_active`: ISO timestamp of the newest turn (first timestamped record
  on the backward walk). `""` if absent.
- `transcript_bytes`: `os.path.getsize(path)` (one stat; the file is already
  open). `0` if absent.

New return: `{last_prompt, model, last_active, transcript_bytes}`. The
`_empty_facts` default and `assemble_sessions`'s injected `tail_facts`
signature update accordingly. This one change feeds F2 and T-A.

---

## F1 — `crr recall` (print-only transcript search)

**What:** retrieve earlier conversation from a session's transcript on demand,
so compaction-dropped detail is a lookup away.

**Interface:** `crr recall [--pid PID | --sid SID] [--all] [-n K] [-C N] <query>`
- Default scope: the transcript for `--pid` (sid resolved from the journal) or `--sid`. `--all` searches every transcript in that cwd's project dir.
- Prints up to `K` (default 5) matching real exchanges, **most-recent-first**, each with `N` lines of surrounding context (`-C`, default the matching turn only). Case-insensitive substring; `--regex` opt-in later.
- Retrieval-only (**print**). No re-injection — feeding recalled text into a live session adds tokens and can trigger the very compaction we're fighting.

**Architecture:**
- Core (`crr/core/transcript.py`): `search(records, query, *, cap, context) -> list[Match]` — pure, testable with synthetic records; reuses `extract_prompt`/`clean_display`. A `Match` carries the turn text + optional adjacent context + an index for ordering.
- Adapter (`crr/adapters/transcript_source.py`): `search_transcript(session_id_or_path, query, ...)` (reads the file; on-demand, not the poll path, so a full scan is fine) and an `--all` variant over `list_transcripts(cwd)`.
- CLI: `_cmd_recall`. No contract/page change (CLI-only).

**Config:** `recall_match_cap` (default 5), `recall_snippet_cap` (chars, default 500).

**Tests:** pure `search` (matches, ordering, cap, context, no-match, noise-skip); adapter over a synthetic transcript; CLI (`--pid` resolves sid; `--all`; empty result prints a clean "no matches").

---

## F2 — Compaction badge (context pressure)

**What:** show on each card whether the session is near/over its model's
context window, i.e. **"will compact on revive."**

**Architecture:**
- Core (`crr/core/context_pressure.py`, new): 
  - `MODEL_CONTEXT_WINDOWS: dict[str,int]` — a documented **prior** (audit P5). Known: `claude-opus-4-8: 1_000_000`. Unknown-at-write-time (opus-5, sonnet-5, fable-5, sonnet-4-6, haiku-4-5): set conservative values with a `# PRIOR — verify` comment and a `DEFAULT_WINDOW = 200_000` fallback for anything unmapped.
  - `estimate_tokens(transcript_bytes) -> int`: `bytes // 4` (rough; **labeled an estimate**).
  - `pressure(transcript_bytes, model) -> str`: fraction = est / window(model); returns `"ok"` (<0.7), `"tight"` (0.7–1.0), `"will-compact"` (≥1.0). Thresholds from config.
- `assemble_sessions` computes `context_pressure` per card from the facts' `transcript_bytes` + `model`.
- **Contract:** add `context_pressure` to `SESSION_CARD_KEYS`; bump `SESSIONS_CONTRACT_VERSION` (v3→v4) + validator (enum: ok/tight/will-compact) + test.
- **Dashboard:** small badge per card — green (ok) / amber (tight) / red "will compact on revive". `PAGE_VERSION` bump. `textContent`.

**Config:** `context_tight_fraction` (0.7), `context_compact_fraction` (1.0).

**Honest calibration:** the badge is an *estimate* from byte size and a *prior* window map; label it as such in the tooltip/legend. Wrong-but-conservative beats confidently-wrong.

**Tests:** `pressure` thresholds + unknown-model fallback; `estimate_tokens`; contract validator; assemble_sessions emits the field; node gate.

---

## T-A — True recency (fix the "Recent" signal)

**What:** the card's recency reflects **conversation activity**, not shell-prompt
activity. Today `updated` is written by the shim on register/last-cmd, so a
session you're deep inside (no return to the prompt) looks stale even as its
transcript grows (observed: 2-day skew).

**Architecture:**
- `last_active` comes from the shared `read_tail_facts` extension (above).
- **Contract:** add `last_active` to `SESSION_CARD_KEYS` (same v3→v4 bump as F2 — one contract change covers both) + validator (nullable ISO string) + test.
- **Dashboard:** the "Recent" sort keys on `last_active` (fallback to `updated` when empty); cards show it as **relative time** ("2m ago", "3d ago"). `PAGE_VERSION` bump (shared with F2).

**Tests:** `read_tail_facts` returns `last_active` from the newest turn; assemble_sessions surfaces it; sort correctness (client JS covered by node-gate parse + a small logic check).

---

## T-B — Per-project "latest" marker

**What:** when multiple sessions share a cwd, visibly flag the most-recently-active
one (by `last_active`) so stale duplicates are obvious.

**Architecture:** dashboard-only, client-side, using `last_active` from T-A. In
each cwd group, mark the max-`last_active` card with a "latest" chip. No
contract change. `PAGE_VERSION` bump (shared).

**Tests:** node-gate parse; a small JS logic check that the latest-per-cwd is chosen correctly.

---

## Terminology change: `detmux` → `untrack`, restore = `retrack`

Unify the vocabulary around **tracking**: crr *tracks* sessions.
- **`untrack`** (renamed from the existing `detmux`): stop tracking a session
  — archive it and re-home it into a visible tab. Same behavior as today's
  `detmux`; only the name/label change.
- **`retrack`** (new, F3): resume tracking — pull an untracked (archived)
  session back into crr.

Back-compat (this renames a *shipped* command):
- Keep `detmux` as a **deprecated alias** of `untrack` (like `restore`→`reopen`).
- Archive reason: write **`"untracked"`** going forward; keep **`"detmuxed"`**
  valid in `ARCHIVE_REASONS` so existing archived records still validate, and
  `retrack` matches **both** reasons.
- Dashboard button `de-tmux` → **Untrack**; restore button → **Retrack**.
- Update help text, tests, and docs referencing detmux.

## F3 — `retrack`: restore untracked sessions

**What:** undo an untrack — move archived `"untracked"`/`"detmuxed"` records
back into crr's active tracking so they show on the dashboard and are revivable.

**Behavior (your choice: re-track as recoverable):** for each target, read the
archive record, write its `entry` back to the journal (active), and
`archive.remove(sid)`. The re-journaled entry's original pid is typically
dead → it classifies `crashed` → appears on the dashboard, revivable via
Reopen/watchdog. Nothing is force-spawned.

**Interface (both CLI + dashboard):**
- CLI: `crr retrack [--last N] [--sid SID]` — `--last N` (default 10) restores the N most-recent untracked records by `archived_at`; `--sid` restores one.
- Web: `GET /api/untracked` → the last 10 untracked records (sid8, cwd, archived_at, last_prompt). Dashboard shows them in a "Recently untracked" list with a per-item **Retrack** button.

**Architecture:**
- Core op (`crr/core/ops.py`): `retrack(store, archive, sid, now) -> OpResult` (single-sid, classifier-agnostic — it's un-archiving, not signalling). A cli/web helper batches `--last N`. Matches both `"untracked"` and legacy `"detmuxed"` reasons.
- **Action protocol:** the existing `/api/action` is pid-keyed (`op`, `pid`). `retrack` is **sid-keyed**. Extend the POST validator to accept a `retrack` op carrying `sid` (string) instead of `pid` — carefully, preserving the strict validation for the pid-keyed ops. Add `retrack` to a sid-keyed action set.
- **Web:** new `GET /api/untracked`; new action branch.
- **Dashboard:** the list + Retrack buttons. `PAGE_VERSION` bump.

**Tests:** `retrack` re-journals + removes from archive; matches both reasons; refuses a non-untracked / missing sid; batch `--last N` ordering; `untrack` (renamed) still archives + re-homes and its `detmux` alias works; `/api/untracked` shape; POST `retrack` sid validation (and that a pid-keyed op still rejects a missing pid); node gate.

---

## T-C — Surface untracked transcripts (session discovery)

**What:** transcripts that exist on disk but aren't in crr's journal (like
`6e262205`) are invisible today. Surface them as **discoverable** sessions you
can adopt into crr.

**Architecture:**
- Core (`crr/core/discovery.py`, new): given the set of journaled sids + a
  transcript listing, return transcripts **not** tracked (sid8, cwd,
  last_active, transcript_bytes, last_prompt), sorted by recency. Pure;
  testable with fakes.
- Adapter: enumerate `~/.claude/projects/*/*.jsonl` → (sid, cwd, mtime); the
  cli passes the journaled sids to the core filter.
- CLI: `crr discover` — list untracked transcripts (recency-sorted).
- **Adopt:** reuse F3's `readopt` mechanism conceptually — "adopt" writes a
  journal entry built from the transcript (sid, cwd, sid_source=`guessed`,
  the shell pid unknown/dead → recoverable). Shares the "create a recoverable
  journal entry from an external record" helper with F3. CLI `crr discover --adopt <sid>`; dashboard "Discoverable" list with an Adopt button.
- **Web:** `GET /api/discoverable`; an `adopt` sid-keyed action (same protocol extension as F3).
- **Dashboard:** a "Discoverable (untracked)" section. `PAGE_VERSION` bump.
- **Contract:** `/api/discoverable` is a new payload — give it its own small validator + version, or fold into a documented shape.

**Honest note:** an untracked transcript may belong to a *live* process crr
can't attach to; adoption creates a *recoverable* entry (revivable via
`--resume`), it does not attach to a running process. Say so in the UI.

**Tests:** core filter (excludes journaled, sorts by recency); adapter enumeration; `crr discover`; adopt creates a valid journal entry; `/api/discoverable` shape.

---

## Delivery — three mergeable slices

1. **Slice A — recency + pressure (F2 + T-A + T-B).** One shared
   `read_tail_facts` extension (`last_active`, `transcript_bytes`), one
   contract bump (v3→v4: `+last_active`, `+context_pressure`), one page bump
   (relative-time recency, Recent-sort fix, compaction badge, latest marker).
   Cohesive; ships the fix for "which session is latest" + "will it compact."
2. **Slice B — `crr recall`.** CLI-only, independent, no contract/page change.
3. **Slice C — retrack + discovery (F3 + T-C), incl. the `detmux`→`untrack`
   rename.** Rename the shipped `detmux` command to `untrack` (deprecated
   `detmux` alias; button relabel; write `"untracked"` archive reason,
   accept legacy `"detmuxed"`). Then `retrack` + `discover`/adopt, sharing a
   "recoverable entry from an external record" helper, the sid-keyed
   action-protocol extension, two new GET payloads (`/api/untracked`,
   `/api/discoverable`), and dashboard lists. One page bump.

Each merges on local-CI-green. Build subagent-driven, per-task review, opus
whole-branch review, re-verify HEAD before merge.

## Acceptance criteria

- **Recency:** a session active minutes ago sorts above one idle for days, using transcript activity — even if its `updated` is stale. Cards show relative last-active; the latest per cwd is marked.
- **Compaction badge:** a session whose estimated tokens exceed its model window shows "will compact on revive"; a small one shows "ok". Windows are documented priors; the estimate is labeled.
- **Recall:** `crr recall --pid P "<phrase>"` prints the matching earlier exchanges (capped, recent-first) and nothing else; `--all` widens to the project; no matches → clean message. Never re-injects.
- **Untrack/Retrack:** `crr untrack` (with `detmux` still working as a deprecated alias) archives + re-homes as before; `crr retrack --last 10` moves the 10 most-recent untracked records back to the journal (removed from archive), where they appear and are revivable; the dashboard offers the same per-item. Refuses non-untracked/missing sids; legacy `"detmuxed"` records are retrackable.
- **Discovery:** `crr discover` lists on-disk transcripts not in the journal (like `6e262205`), recency-sorted; adopt creates a revivable entry. UI states adoption ≠ attaching to a live process.
- Layering `KEPT`; contracts bumped with validators; `PAGE_VERSION` bumped per page change; node gate green.
