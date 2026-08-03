# Slice A — Recency + Compaction Pressure (Implementation Plan)

> Execute subagent-driven, task-by-task, TDD. Checkbox steps.

**Goal:** Cards carry true conversation recency and a context-pressure badge, so the latest session is obvious and "will compact on revive" is visible. Implements T-A (true recency), T-B (latest marker), F2 (compaction badge) from `docs/superpowers/specs/2026-08-02-session-recovery-recall-ux-design.md`.

**Base:** branch `feat/session-recovery-ux` (has the spec). Current HEAD family `f277e80`.

## Global Constraints
- Layering `cli→adapters→core`; `.venv/bin/lint-imports` = `KEPT`. stdlib only. TDD (watch red first).
- Contract change → bump `SESSIONS_CONTRACT_VERSION` (v3→v4) + validator + tests. Page change → bump `PAGE_VERSION` + keep `node --check` gate green (`pytest -k node`).
- A **pre-commit hook runs the full suite + lint** — every commit must be green.
- Honest calibration: token counts are *estimates*; model windows are *priors*.
- Read the CURRENT code before editing; match existing patterns.

---

### Task A1: Extend the tail-facts reader with `last_active` + `transcript_bytes`

**Files:** `crr/core/transcript.py`, `crr/adapters/transcript_source.py`, `crr/core/status.py` (`_empty_facts` default), tests `tests/test_transcript.py`, `tests/test_transcript_source.py`.

**Deliverable:**
- `crr/core/transcript.py::tail_facts(records, *, cap)` → the returned dict gains `"last_active"` = the ISO `timestamp` of the newest record that has one (`""` if none). (It already returns last_prompt/model.)
- `crr/adapters/transcript_source.py::read_tail_facts(...)` → returned dict gains `"last_active"` (first timestamped record on the backward walk) and `"transcript_bytes"` (`path.stat().st_size`; `0` when absent). Keep the single-pass/early-exit shape; grabbing `last_active` is free on the backward walk (the first record seen IS the newest).
- Update the `_empty_facts` default (in `crr/core/status.py`) to `{"last_prompt":"", "model":"", "last_active":"", "transcript_bytes":0}`.

**Interfaces produced:** `read_tail_facts(...) -> {last_prompt, model, last_active, transcript_bytes}`.

**Steps:**
- [ ] Read current `tail_facts` + `read_tail_facts` + `_empty_facts`.
- [ ] Write failing tests: core `tail_facts` returns newest `last_active`; adapter `read_tail_facts` returns `last_active` (newest turn) + `transcript_bytes` = file size, and empties/0 when transcript absent. Watch fail.
- [ ] Implement. Keep the backward early-exit; get size via the opened path.
- [ ] Green + `lint-imports` KEPT. Commit.

---

### Task A2: `context_pressure` core (model window prior + estimate + level)

**Files:** create `crr/core/context_pressure.py`; `crr/core/config.py` (two thresholds); tests `tests/test_context_pressure.py`, `tests/test_config.py`.

**Deliverable:**
- `MODEL_CONTEXT_WINDOWS: dict[str,int]` — documented **prior** (audit P5). Set `"claude-opus-4-8": 1_000_000` (confirmed). For the others present in the wild (`claude-opus-5`, `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `claude-fable-5`) add entries **with a `# PRIOR — verify` comment**; use `200_000` where genuinely unsure. `DEFAULT_WINDOW = 200_000` for unmapped models.
- `estimate_tokens(transcript_bytes: int) -> int` = `transcript_bytes // 4` (rough; docstring says "estimate").
- `window_for(model: str) -> int` (map lookup w/ default).
- `pressure(transcript_bytes: int, model: str, *, tight: float, compact: float) -> str` → `"ok"` / `"tight"` / `"will-compact"` by fraction = est/window.
- Config `DEFAULTS`: `"context_tight_fraction": 0.7`, `"context_compact_fraction": 1.0` (bump `CONFIG_DEFAULTS_VERSION`).

**Steps:**
- [ ] Read `crr/core/config.py` DEFAULTS + version constant.
- [ ] Failing tests: pressure thresholds (ok/tight/will-compact incl. boundaries), unknown-model→default window, estimate_tokens, config defaults present. Watch fail.
- [ ] Implement module + config defaults. Green + KEPT. Commit.

---

### Task A3: Card fields `last_active` + `context_pressure`; contract v3→v4

**Files:** `crr/core/contracts.py`, `crr/core/status.py`, and fixture updates across `tests/test_contracts.py`, `tests/test_status.py`, `tests/test_web.py`, `tests/test_e2e_linux.py` (any that build a session card or assert the contract version).

**Deliverable:**
- `contracts.py`: add `"last_active"` and `"context_pressure"` to `SESSION_CARD_KEYS`; bump `SESSIONS_CONTRACT_VERSION` 3→4; in `validate_session_card` require `last_active` (str, nullable→treat "" as valid str) and `context_pressure` (enum: `"ok"`/`"tight"`/`"will-compact"`). Add validator tests (missing field rejected; bad enum rejected).
- `status.py::assemble_sessions`: pull `last_active` + `transcript_bytes` from `facts`; compute `context_pressure = crr.core.context_pressure.pressure(facts["transcript_bytes"], facts["model"], tight=..., compact=...)` — thresholds passed in from the caller (cli reads config) OR read via an injected callable to keep core pure. Simplest: add `pressure` params to `assemble_sessions` (defaulted) OR compute in cli. **Keep core pure** — pass the two fractions into `assemble_sessions` (defaulted to the config defaults' values is fine as literals mirrored, or accept a small `pressure_fn`). Add `last_active` + `context_pressure` to the card dict.
- Update every card-building test fixture to include the two new fields, and any assertion of the contract version (3→4).

**Steps:**
- [ ] Grep the fixtures/asserts that will break (`SESSION_CARD_KEYS`, `SESSIONS_CONTRACT_VERSION`, `_session_card`, `assemble_sessions(`).
- [ ] Failing tests first (new validator tests + updated fixtures). Watch fail.
- [ ] Implement contract + assemble_sessions. Green (full suite) + KEPT. Commit.

---

### Task A4: Dashboard — recency sort + relative time + badge + latest marker; PAGE_VERSION bump

**Files:** `crr/core/page.html`, `crr/core/web.py` (`PAGE_VERSION` bump), `tests/test_web.py` (node gate + any version assert).

**Deliverable (page.html):**
- "Recent" sort keys on `last_active` (fallback to `updated` when `last_active` is empty).
- Each card shows last activity as **relative time** ("just now", "2m ago", "3d ago") from `last_active` (fallback `updated`), via a small `relTime(iso)` helper.
- **Compaction badge** per card from `context_pressure`: `ok`→subtle/none or green dot, `tight`→amber, `will-compact`→red "will compact on revive". `textContent`. Add to the key/legend.
- **Latest marker (T-B):** within each cwd, mark the card with the max `last_active` with a small "latest" chip.
- `web.py`: bump `PAGE_VERSION` (current 14 → 15) with a comment.

**Steps:**
- [ ] Read the current `page.html` render/sort/group JS + the key/legend + `web.py` PAGE_VERSION.
- [ ] Implement the four page changes + bump. (Untrusted fields via `textContent`.)
- [ ] `pytest -k node` green (JS parses); `pytest tests/test_web.py` green (version assert updated); full suite + KEPT. Commit.

---

## Slice A done
- [ ] Full suite + `lint-imports` KEPT + `node --check` gate all green.
- [ ] Opus whole-branch review; fix wave if needed.
- [ ] Re-verify HEAD unchanged; merge `feat/session-recovery-ux` → main local-CI-green + `gh pr create`.
