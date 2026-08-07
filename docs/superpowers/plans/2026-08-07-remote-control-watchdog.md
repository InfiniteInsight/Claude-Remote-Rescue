# Plan — Part B: dropped-Remote-Control watchdog

> Execute subagent-driven, TDD (watch red first). Read CURRENT code before
> editing. A pre-commit hook runs the full suite + lint — the tree must be
> green to commit. Layering `cli → adapters → core`; `lint-imports` = `KEPT`.

**Spec:** `docs/superpowers/specs/2026-08-07-remote-control-watchdog-design.md`
— read it first; it carries the measurements and the two-level toggle truth
table.

**Base:** branch `feat/remote-control-watchdog` off `main`.

**Starting versions:** `SESSIONS_CONTRACT_VERSION = 6`,
`CONFIG_DEFAULTS_VERSION = 11`, `PAGE_VERSION = 38`.

---

### Slice 1 — detection: core + adapter + card contract

**Files:** create `crr/core/bridge.py`; `crr/adapters/transcript_source.py`;
`crr/core/contracts.py`; `crr/core/status.py`; `crr/core/config.py`; tests.

1. `crr/core/bridge.py::bridge_state(records_since_marker, had_marker, *, stale_after) -> str`
   returning `"off"` / `"ok"` / `"dropped"`. Pure, no I/O, mirroring
   `takeover.ready_to_take_over`'s shape. `had_marker=False` -> `"off"`
   regardless of count (you cannot drop what was never up).
2. Adapter: extend the existing backward walk in `read_tail_facts` to also
   report `bridge_since` (records seen before the newest `bridge-session`
   record) and `bridge_seen` (bool). Measured: the newest marker sits 0–11
   records from the tail on healthy sessions and never more than 67 behind,
   so bound the search with a new config key `bridge_scan_lines` (default
   400, same shape as `reply_tail_lines`) and report `bridge_seen=False`
   beyond it — an honest "unknown", never a fabricated drop.
3. Config keys: `remote_control_watch` (bool, True), `remote_control_autokick`
   (bool, True), `bridge_stale_records` (int, 150), `bridge_scan_lines`
   (int, 400). Bump `CONFIG_DEFAULTS_VERSION` 11 -> 12.
4. Card: add `remote_control` (enum off/ok/dropped) to `SESSION_CARD_KEYS` +
   validator; `status.assemble_sessions` computes it via `bridge.bridge_state`
   with the threshold passed in from the caller (core stays pure — mirror how
   `context_pressure` thresholds are injected). Bump
   `SESSIONS_CONTRACT_VERSION` 6 -> 7 and fix every card fixture.

**Do NOT** add the `autokick` card field here — it depends on Slice 2's
store. Slice 3 bumps the contract again if needed, or Slice 2 adds it.

**Tests:** bridge_state boundaries incl. `had_marker=False`; adapter counts
correctly on a fake transcript, reports unseen beyond the window, degrades
on an unreadable file; contract validator + version; assemble_sessions
emits the field.

---

### Slice 2 — the settings store and the watchdog action

**Files:** create `crr/core/settings.py`; `crr/cli.py`; tests.

1. **Generalise the dashboard-managed store.** `crr/core/exclusions.py`
   already owns a JSON file in the state dir with atomic writes and
   degrade-to-default reads. Add a sibling `crr/core/settings.py` holding:
   - a global bool `autokick` (dashboard override of the config default), and
   - a per-**session-id** map of bool overrides.
   Keyed by sid, NEVER pid (a recycled pid would transfer an opt-out to an
   unrelated session). Reuse `journal.write_json_atomic` /`read_json_file`.
   A missing or corrupt file degrades to "no overrides", never raises.
2. **Resolution helper** (pure): `settings.autokick_for(sid, *, config_default,
   global_override, session_override) -> bool` implementing the spec's truth
   table exactly — global OFF wins over everything; per-session values are
   retained while global is off.
3. **The watchdog step** in `_cmd_revive`: a SEPARATE pass, clearly gated,
   after the existing revival work. For each **LIVE** session whose
   `remote_control == "dropped"` and whose resolved autokick is true: only
   kick when `takeover.ready_to_take_over` says the transcript is at a
   completed assistant turn boundary; otherwise leave it for the next pass.
   Reuse `ops.kick`. Log what it did and what it skipped, with the reason.

**Tests:** the four truth-table rows; sid-keyed (not pid) overrides; a
dropped+ready LIVE session is kicked; a dropped-but-mid-turn one is NOT;
an `off`/`ok` session is never kicked; a CRASHED session is untouched by
this path; global off kicks nothing; corrupt store -> config default.

---

### Slice 3 — surfacing and control in the dashboard

**Files:** `crr/core/web.py`; `crr/core/page.html`; `crr/cli.py`; tests.

1. **Badge**: `remote control dropped` on the card, same family as the
   pressure badges (`off` renders nothing — most sessions).
2. **Global toggle** in the Settings modal, next to the exclusions editor,
   through the existing exclusions-style endpoint pattern (host allowlist,
   JSON content-type gate, atomic write, validated type). Copy states that
   turning it off keeps the badge.
3. **Per-session toggle** on the card, via the existing `/api/sid-action`
   namespace (a new op). It renders **disabled with the reason** when the
   global switch is off — never an ON it cannot honour.
4. Card gains `autokick` so the toggle shows true state; bump
   `SESSIONS_CONTRACT_VERSION` if Slice 2 did not already.
5. `PAGE_VERSION` bump; node gate green.

**Tests:** badge appears only for `dropped`; Settings toggle round-trips;
sid-action op validates and dispatches; the card toggle renders disabled
when global is off; page-string assertions.

---

## Done
- [ ] Full suite + `lint-imports` KEPT + node gate green.
- [ ] Opus whole-branch review; fix wave.
- [ ] Re-verify `origin/main` unmoved; merge local-CI-green; push; restart
      `crr-web.service` (page changed).
- [ ] Report what was verified live vs unit-tested only. In particular: a
      real dropped bridge is hard to stage, so say plainly whether the
      auto-kick path was exercised end to end or only with fakes.
