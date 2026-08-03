# Slice C — untrack rename + retrack + discovery (Implementation Plan)

> Execute subagent-driven, TDD. Read CURRENT code before editing (this repo evolves). A pre-commit hook runs the full suite + lint on every commit — tree must be green.

**Goal:** rename `detmux`→`untrack`; add `retrack` (undo untrack); add `discover`/adopt for untracked transcripts. Implements F3 + T-C + the terminology change from `docs/superpowers/specs/2026-08-02-session-recovery-recall-ux-design.md`.

**Base:** branch `feat/session-recovery-slice-c` off `main` (`a8e14c3` family).

## Global constraints
- Layering `cli→adapters→core`; `lint-imports` KEPT. stdlib only. TDD (red first).
- Contract change → bump `SESSIONS_CONTRACT_VERSION`/add validator + tests. Page change → bump `PAGE_VERSION`, node gate green.
- **Do NOT weaken** the existing pid-keyed `/api/action` strict validation. Untrusted fields via `textContent`.
- Sid inputs must pass `contracts.valid_session_id`.

---

### Task C1: Rename `detmux` → `untrack` (deprecated alias; new archive reason)

**Files:** `crr/cli.py`, `crr/core/ops.py`, `crr/core/web.py` (`ACTIONS`), `crr/core/page.html` (button label), `crr/core/contracts.py` (`ARCHIVE_REASONS`), `README.md`, tests.

**Deliverable:**
- CLI: primary command **`untrack`** with **`detmux`** as a deprecated alias (use argparse `aliases=["detmux"]`, mirroring how `reopen` has alias `restore`). Help text: "stop tracking a session — archive it and re-home into a visible tab".
- `crr/core/ops.py`: the op may keep its function name or rename to `untrack` (your call; if rename, keep behavior identical and update all callers/tests). It must now archive with reason **`"untracked"`** (add `"untracked"` to `contracts.ARCHIVE_REASONS`; KEEP `"detmuxed"` valid for existing records).
- `web.py` `ACTIONS`: add `"untrack"` (keep `"detmux"` too for the alias/back-compat).
- `page.html`: the existing de-tmux button label → **"Untrack"** (the action op it POSTs can stay `detmux` or become `untrack` — keep the server accepting both). `PAGE_VERSION` bump (coordinate: C4 also bumps — if C1 lands first, bump here and let C4 bump again).
- Update `README.md` command table + any docs referencing detmux; update tests referencing detmux/"detmuxed".

**Steps:** read current `detmux` cli/op/tests; TDD (a test that `untrack` archives with reason "untracked" and that the `detmux` alias still works); implement; full suite + KEPT; commit.

---

### Task C2: `retrack` — restore untracked sessions (core op + CLI + GET + sid-action endpoint)

**Files:** `crr/core/ops.py`, `crr/core/archive.py` (a helper if useful), `crr/cli.py`, `crr/core/web.py`, tests.

**Deliverable:**
- Core op `retrack(store, archive, sid, now) -> OpResult`: read the archive record for `sid`; if absent or its reason is not in `{"untracked","detmuxed"}` → refuse; else write `record["entry"]` back to the journal (`store.write`) and `archive.remove(sid)`. Return ok with a message.
- A batching helper (cli-level ok): the N most-recent untracked/detmuxed archive records by `archived_at`.
- CLI: `crr retrack [--last N] [--sid SID]` — `--last N` (default 10) retracks the N most recent; `--sid` one. Under `mutation_lock`.
- Web GET **`/api/untracked`**: return the last 10 untracked/detmuxed archive records as `{sid8, session_id, cwd, archived_at, last_prompt}` (last_prompt via the entry or transcript; keep it cheap). Add a tiny validator/shape test.
- **Sid-keyed action endpoint** (new, keeps `/api/action` untouched): POST **`/api/sid-action`** with JSON `{op, sid}`. Validate: `op` in a new `SID_ACTIONS = ("retrack",)` (extended in C3), `sid` passes `contracts.valid_session_id`. Same CSRF posture (JSON content-type gate, no CORS, host allowlist — reuse `handle_request`'s existing gates; add the route branch). Dispatch to an injected `sid_action_provider(op, sid) -> (ok, message)`. The cli `_cmd_web` wires `sid_action_provider` calling `ops.retrack` under `mutation_lock`.
- Tests: `retrack` re-journals + removes from archive; refuses non-untracked/missing sid; matches both reasons; batch ordering; `/api/untracked` shape; `/api/sid-action` validation (rejects bad op, non-uuid sid, non-JSON) and dispatch; the existing pid `/api/action` still strict.

**Steps:** read current web.py routing + `_cmd_web` provider wiring + archive scan; TDD; implement; full suite + KEPT; commit.

---

### Task C3: Discovery (T-C) — surface + adopt untracked transcripts

**Files:** create `crr/core/discovery.py`; `crr/adapters/transcript_source.py` (enumerate transcripts w/ cwd + recency); `crr/cli.py`; `crr/core/web.py`; tests.

**Deliverable:**
- Core `crr/core/discovery.py`: `untracked(journaled_sids: set[str], transcripts: list[dict]) -> list[dict]` — return transcripts whose `session_id` is NOT journaled, each `{session_id, sid8, cwd, last_active, transcript_bytes, last_prompt}`, sorted most-recent-first. Pure.
- Adapter: an enumerator over `~/.claude/projects/*/*.jsonl` yielding `{session_id, cwd (decoded from the project dir name), mtime}` (reuse/extend `list_transcripts`/the encoding helper). The cli passes journaled sids (from `JournalStore.scan`) into the core filter.
- CLI: `crr discover` — list untracked transcripts (recency-sorted, sid8 + cwd + relative age + last_prompt). `crr discover --adopt SID` — **adopt**: build a journal entry from the transcript (sid, cwd, `sid_source="guessed"`, a placeholder/dead pid, `claude` populated, `tmux_session=None`) and `store.write` it, so it appears as a recoverable card. Reuse a shared "recoverable entry from external record" helper with C2 if clean. Print a clear note: adoption creates a *recoverable* entry (revivable via reopen), it does NOT attach to a live process.
- Web GET **`/api/discoverable`**: the untracked list (shape test). Extend `SID_ACTIONS` with `"adopt"`; `sid_action_provider` handles `adopt` (build+write the entry). Adoption needs the transcript's cwd → the provider resolves it from the discovery enumerator.
- Tests: core `untracked` filter (excludes journaled, recency sort, empty); adapter enumeration under fake HOME; `crr discover` lists; `--adopt` writes a valid journal entry (passes `validate_journal_entry`); `/api/discoverable` shape; `adopt` via `/api/sid-action`.

**Steps:** TDD; implement; full suite + KEPT; commit.

---

### Task C4: Dashboard — untracked + discoverable lists; button relabel; PAGE_VERSION

**Files:** `crr/core/page.html`, `crr/core/web.py` (`PAGE_VERSION`), tests.

**Deliverable:**
- A **"Recently untracked"** collapsible section (fetched from `/api/untracked`, lazy like the diagnostics panel — not on the poll path) listing sid8 + cwd + age + last_prompt with a per-item **Retrack** button → POST `/api/sid-action {op:"retrack", sid}`.
- A **"Discoverable (untracked)"** section from `/api/discoverable` with per-item **Adopt** button → POST `/api/sid-action {op:"adopt", sid}`, and a one-line note that adoption ≠ attaching to a live process.
- Ensure the session-card action button reads **"Untrack"** (from C1).
- `PAGE_VERSION` bump (final value; coordinate with C1). `textContent` for cwd/last_prompt/sid.
- node gate green; `test_web` version assert updated.

**Steps:** read current page.html (its lazy-panel pattern for diagnostics, the POST helper); implement; node gate + full suite + KEPT; commit.

---

## Slice C done
- [ ] Full suite + `lint-imports` KEPT + node gate green.
- [ ] Opus whole-branch review; fix wave if needed.
- [ ] Re-verify `origin/main` unmoved; merge → main local-CI-green + push. Restart `crr-web.service` to deploy the page changes.
