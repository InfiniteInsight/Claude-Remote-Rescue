# plumb-line audit — Claude-Remote-Rescue

```
report-format: v3
scope:               repository
principles-revision: 1
date:                2026-07-31
commit:              167bb65
```

## Principle glossary

- P3 — Confidence + provenance
- P5 — Injectable priors
- P7 — Contracted outputs
- P8 — State-first lineage
- P9 — Golden baseline + explain-the-drift
- spine — null-result expressibility

## Findings

| Path | Line | Function | Issue | Suggested Fix | Principle |
|---|---|---|---|---|---|
| `crr/core/status.py` | 1 | module docstring | docstring says "contract v2"; shipped `SESSIONS_CONTRACT_VERSION = 3` | update docstring to v3 | P9 — Golden baseline + explain-the-drift |
| `crr/core/page.html` | 254, 296 | `addBtn`, `showNotice` | confirm-disarm (4000ms) and notice duration (3000ms) hardcoded where sibling intervals are config-injected | DEFAULTS keys + placeholders mirroring POLL_MS | P5 — Injectable priors |
| `crr/core/page.html` | 416 | `renderDiag` | `errs.slice(0, 20)` un-named client-side display cap | source from config/payload | P5 — Injectable priors |
| `crr/core/page.html` | 374 | `pollVersion` | stale-page reload delay `800` hardcoded | add to DEFAULTS | P5 — Injectable priors |
| `crr/cli.py` | 350 | `_cmd_doctor` | `timeout=5` literal duplicates `interop_timeout_seconds` | read config | P5 — Injectable priors |
| `crr/cli.py` | 150–192 | `_build_parser` | `default=8377` repeated in four `--port` definitions | `dashboard_port` DEFAULTS key ×4 | P5 — Injectable priors |
| `crr/adapters/systemd.py` | 143 | `web_service_unit` | `RestartSec=2` baked, unconfigurable | config key threaded like watchdog interval | P5 — Injectable priors |
| `crr/cli.py` | 301–306 | `_cmd_doctor` | prints 3 of 6 declared contract versions (omits archive, config-defaults, page) | print all six | P7 — Contracted outputs |
| `crr/cli.py` | 1362–1369 | `_cmd_config` | `--effective` never prints `CONFIG_DEFAULTS_VERSION` (zero consumers) | print it | P7 — Contracted outputs |
| `crr/adapters/systemd.py`+`launchd.py`+`scheduled_task.py`+`crr/shims/*` | — | generators | generated units/plists/tasks/shims carry no version stamp; stored artifacts all stamp `"v"` | embed crr version + CONFIG_DEFAULTS_VERSION comment | P7 — Contracted outputs |
| `crr/core/diagnostics.py` | 93–121 | `build_payload` | payload omits generating caps/lookback/timeouts — not regenerable later | record generating values or config version | P5 — Injectable priors / P3 — Confidence + provenance |
| `crr/core/page.html` ~382–418; `crr/cli.py` 1015–1034 | — | `renderDiag`, `_cmd_diagnose` | payload's `source`/`boots` lineage never rendered by either consumer | render source + boot identity in both | P3 — Confidence + provenance |
| `crr/cli.py` | 395–403 | `_print_status_human` | certain/guessed duplicates collapsed to `[dup]`; sid_source dropped (dashboard renders the distinction) | print sid_source / uncertain-dup qualifier | P3 — Confidence + provenance |
| `crr/cli.py` | 636–640, 1053–1054 | `_cmd_revive`, `_cmd_gc` | needs-review: gave-up / gc-removed reported as bare counts while sibling problem-loops name files | name pids/sids for terminal outcomes | P8 — State-first lineage |
| `crr/core/archive.py` / `crr/core/web.py` | — | — | needs-review: archive lineage captured but no human read path (no CLI list / API / dashboard) | `crr archive --list` and/or `/api/archive` | P8 — State-first lineage |
| `crr/adapters/tmux.py` | 45–57 | `list_sessions` | needs-review: query failure and empty both collapse to `set()`; transient failure can accumulate a revive strike | tri-state return (unknown vs empty) | spine — null-result expressibility |
| `crr/adapters/process_probe.py` | 173 | `terminate_group` | needs-review: `sleep(0.1)` poll granularity (bounded by config grace) | name it or leave | P5 — Injectable priors |
| `crr/adapters/transcript_source.py` | 86 | `MODEL_TAIL_LINES` | needs-review: `= 200` scan bound, empirically justified in-comment, not injectable | consider promoting to DEFAULTS | P5 — Injectable priors |
| repo-wide | — | — | advisory adoption gap (declared `planned`): no golden baseline; validators serve as pins | tracked in AGENTS.md declaration | P9 — Golden baseline + explain-the-drift |

## Omission-pass enumeration (all surfaces walked)

| Surface | provenance? | confidence? | lineage (reproduce)? | versioned contract? | null/absence? | golden baseline? |
|---|---|---|---|---|---|---|
| Journal entry file (stored) | yes — `claude.sid_source` | yes — sid_source | yes — raw source-of-truth | yes — v1 | yes — nullable fields | no (planned) |
| Archive record file (stored) | yes — `reason` enum | partial — inherits sid_source | yes — verbatim entry | yes — v1 | n/a | no (planned) |
| Relaunch/close flag file | n/a — opaque, exempt by docstring | n/a | partial — no timestamp | no — deliberately unversioned | yes — absence = default | n/a |
| Rescue prompt marker | n/a — opaque, exempt | n/a | n/a — boot_id is filename | no — deliberate | yes — existence is signal | n/a |
| /api/sessions payload+card | yes | yes — dup weighting | partial — view only | yes — v3 | yes | no (planned) |
| /api/diagnostics | partial — `source`, but see findings | no | no — caps not recorded | yes — v2 | yes — degraded list | no (planned) |
| /api/version | n/a | n/a | n/a | partial — no dedicated validator | n/a | n/a |
| Served page HTML | yes — sid_source badges | yes | no — drops source/boots | PAGE_VERSION + self-heal | yes — empty state | no (planned) |
| `crr status` human | no — sid_source unprinted | no | n/a | renders v3 | yes | no |
| `crr status --json` | yes | yes | partial | yes — v3 | yes | no |
| `crr rescued` | no | no | partial | no — ad hoc | yes | no |
| `crr diagnose` human | no | no | no | n/a | yes | no |
| `crr diagnose --json` | partial | no | no | yes — v2 | yes | no |
| `crr config --effective` | yes — origin per key | n/a | n/a | no — version unprinted | n/a | no |
| `crr doctor` | partial | no | no | partial — 3 of 6 versions | yes | no |
| `crr revive` summary | no | no | no — bare counts | n/a | yes — explicit 0 | no |
| OpResult messages | partial | n/a | yes for degrade paths | no (undeclared) | yes — refusal reasons | no |
| shim repair-check protocol | n/a | n/a | yes | no — deliberate | yes — empty = absent | n/a |
| `crr shim` output | n/a | n/a | partial — baked path | no — unstamped | n/a | no |
| systemd/launchd/schtasks artifacts | n/a | n/a | partial — baked values | no — unstamped | n/a | no |

## Coverage map

```
read:      all 41 files under crr/ (core, adapters, shims, page.html, cli.py),
           README.md, DESIGN.md, ROADMAP.md, CHANGELOG.md, AGENTS.md,
           docs/site/index.html, .importlinter, pyproject.toml
partial:   tests/test_cli.py + test_contracts.py (pin-existence only),
           docs/superpowers/specs/2026-07-27-kick-close-repair-loop-design.md,
           git history of page.html (targeted searches)
not-read:  tests/ (25 further files), SECURITY.md, .claude/guards/,
           .github/workflows/, docs/screenshots/, docs/tracking-dialect.md,
           build/ (stale mirror, excluded)
coverage:  ~50/80 in-scope files read (63%); both passes read the full crr/ tree
scope note: findings are drawn from the read set only; a not-read file with no
finding is not a clean file. This audit does not claim completeness.
```

19 findings: 13 violations, 5 needs-review, 1 advisory adoption gap (golden baseline, declared planned).

---

# Run 3 — scoped re-audit of the post-baseline delta (2026-08-07)

```
report-format: v3
scope:               167bb65..4da4e11 (the 119 commits since the run-2 baseline audit)
principles-revision: 1
date:                2026-08-07
commit:              4da4e11
```

## Principle glossary

- P3 — Confidence + provenance
- P4 — Quarantined fakery
- P5 — Injectable priors
- P6 — Maturity vocabulary
- P7 — Contracted outputs
- P8 — State-first lineage
- P9 — Golden baseline + explain-the-drift
- spine — null-result expressibility

P1 — Source-truth layer and P2 — One-way layering: no findings. `lint-imports`
reports `1 kept, 0 broken`; no derived/mock/platform logic entered `crr.core`.

## Findings

| Path | Line | Function | Issue | Suggested Fix | Principle |
|---|---|---|---|---|---|
| `crr/adapters/transcript_source.py` | 529, 123 | `read_tail_facts` | `bridge_seen=False` means BOTH "no bridge was ever enabled" and "no marker within `bridge_scan_lines`" — the docstring calls the latter an honest "unknown", but `bridge.bridge_state` turns both into `"off"`, a positive claim on the card ("Remote Control was never enabled") | tri-state as F16 already did for `tmux.list_sessions() -> set \| None`: return `bridge_seen: True\|False\|None` and add an `unknown` member to `REMOTE_CONTROL_STATES` | spine — null-result expressibility |
| `crr/cli.py` | 1435 | `_discoverable_rows` | `read_cwd(...) or t["cwd"]` collapses the AUTHORITATIVE cwd (stamped on the transcript's own records) with the lossy project-dir decode the adapter's own docstring calls "a DISPLAY/fallback value, not authoritative". The merged string flows into `build_adopted_entry` → journal → `tmux.new_detached_session(..., entry["cwd"], ...)`, where a wrong directory fails to revive | add `cwd_source: verified\|decoded` alongside it, exactly as `sid_source` does for the session id | P3 — Confidence + provenance |
| `crr/core/status.py` | 135–150 | `assemble_sessions` | `context_pressure` is computed from `facts["model"]`, which is `""` on ~1 in 3 transcripts (config.py says so). `window_for("")` silently returns `DEFAULT_WINDOW`, so a badge derived from a fabricated 200K window is indistinguishable from one derived from a confirmed model window | carry the window's origin on the card (or emit `context_pressure: "unknown"` when the model is unknown) | P3 — Confidence + provenance |
| `crr/core/bridge_kicks.py` | 137–152 | `record_kick` | The stored record of a DESTRUCTIVE action against a live process holds only `{attempts, last_kick_ts}` — not the observed `bridge_since`, the pid signalled, the kick's outcome, or the thresholds in force. You cannot later reconstruct why a session was restarted 3 times. Sibling practice exists: `ArchiveStore` records reason + timestamp + cwd | record the justifying observation and the resolved thresholds per attempt | P8 — State-first lineage |
| `crr/core/contracts.py` | 27 | — | 5 served payloads (`/api/discoverable`, `/api/untracked`, `/api/recall`, `/api/exclusions`, `/api/settings`) have no version constant, no validator, no canonical key list — while AGENTS.md declares "Contract shapes are versioned … an unversioned shape change is the exact laundering the audit flagged" | give each a `*_CONTRACT_VERSION` + `validate_*` + key tuple, as sessions/diagnostics have | P7 — Contracted outputs |
| `crr/core/settings.py` · `bridge_kicks.py` · `exclusions.py` | — | store classes | 3 stored-on-disk JSON shapes, read back by later crr builds, carry no version field — the same uncontracted-shape gap, but persisted, so a future shape change is a silent cross-version read | add a `v` key + validator, mirroring `ARCHIVE_CONTRACT_VERSION` | P7 — Contracted outputs |
| `crr/core/status.py` | 70–73 | `assemble_sessions` | Four literal fallback defaults (`0.7`, `1.0`, `150`, `True`) duplicate `config.DEFAULTS`. Run 2b fixed exactly this pattern for `web_restart_seconds`/`model_tail_lines` ("now reference `config.DEFAULTS`") — re-introduced here, and both modules are `crr.core`, so the import is legal | reference `config.DEFAULTS[...]` | P5 — Injectable priors |
| `crr/adapters/transcript_source.py` | 123 | `read_cwd` | `_CWD_SCAN_LINES = 200` is a module constant while EVERY sibling scan bound (`model_tail_lines`, `reply_tail_lines`, `bridge_scan_lines`) was lifted to a versioned config key with its measurement recorded | add `cwd_scan_lines` to DEFAULTS; bump `CONFIG_DEFAULTS_VERSION` | P5 — Injectable priors |
| `crr/core/context_pressure.py` | 22, 43 | `estimate_tokens`, `window_for` | `bytes // 4` (chars-per-token) and `DEFAULT_WINDOW = 200_000` are judgment calls baked into logic, while the two thresholds they feed (`context_tight_fraction`/`context_compact_fraction`) are injected config | lift both to DEFAULTS and inject | P5 — Injectable priors |
| `crr/core/web.py` | 169 | — | `DISCOVERABLE_PAGE = 20` is a hardcoded display cap while every sibling display cap (`diag_error_display_cap`, `recall_match_cap`, `recall_snippet_cap`, `last_prompt_display_cap`) is a config key | add `discoverable_page_size` to DEFAULTS | P5 — Injectable priors |
| `crr/core/page.html` | 420 | `flashSid` | `FLASH_MS = 1400` hardcoded beside `CONFIRM_ARM_MS`/`NOTICE_MS`/`RELOAD_DELAY_MS`, which run 2 lifted into `@PLACEHOLDER@` injection for precisely this reason | add a `flash_ms` key + placeholder | P5 — Injectable priors |
| `crr/core/page.html` | 1208 | discoverable filter | `250`ms debounce, same class as above | add a `filter_debounce_ms` key + placeholder | P5 — Injectable priors |
| `crr/core/status.py` | 1 | module docstring | Docstring says "contract v4"; the build ships `SESSIONS_CONTRACT_VERSION = 8`. This is finding F1 of the run-2 audit ("docstring says v2; shipped v3") regressed in the same file | update to v8; consider a test pinning the docstring to the constant so it cannot drift again | P9 — Golden baseline + explain-the-drift |
| `crr/core/contracts.py` | 24–27 | — | The sessions version log documents v4, v7, v8 — **v5 and v6 are absent**, and v3's original explanation ("adds the per-session nullable tmux_session field") was DELETED. Two shape changes shipped with no recorded reason, and one prior reason was destroyed | restore v3's line; add v5/v6 entries naming what each added | P9 — Golden baseline + explain-the-drift |
| `crr/core/config.py` | 39–43 | — | `CONFIG_DEFAULTS_VERSION` log runs v2…v9 then jumps to **v11 — v10 has no entry**. One defaults revision shipped with no recorded reason | add the v10 entry | P9 — Golden baseline + explain-the-drift |
| `AGENTS.md` | 88 | Plumb-line declaration | The declared contract list reads "journal v1 · sessions v3 · diagnostics v2 · archive v1"; shipped are sessions **v8** and diagnostics **v3**, and the list omits the three new dashboard-managed stores entirely. The project's own ruleset overstates how settled its contract surface is | refresh the versions; list the stores (or the P7 gap above, once closed) | P6 — Maturity vocabulary |
| `crr/core/status.py` | 105–112 | `assemble_sessions` | Docstring records a "Known gap, accepted for this slice" — that a card can read `autokick: "on"` while a degraded settings store means nothing is kicked. Commit `b4fe3b6` closed that gap (`effective_global_autokick`), and cli.py:519/1797/2319 pass it. The docstring understates the code's maturity and would send a reader hunting a fixed bug | delete the paragraph, or rewrite it as the residual reason-loss below | P6 — Maturity vocabulary |
| `crr/core/config.py` | 112 | `context_tight_fraction` comment | Calls the context window "(prior, unverified for most models)"; `context_pressure.py`'s docstring now states "Every listed entry is now confirmed against published model docs". One of the two is stale — they cannot both be current | reconcile to the confirmed state | P6 — Maturity vocabulary |
| `crr/core/discovery.py` | 87–96 | `build_adopted_entry` | **needs review** — `host="tab"`/`shell="bash"` are fabricated placeholders written into the journal (a real output path) carrying no marker of their own; only the `ADOPTED_BOOT_ID` sentinel and `sid_source="guessed"` label the entry indirectly, and neither names these two fields | consider `host=None`/`shell=None` if the schema can be widened, or an explicit `fabricated_fields` list | P4 — Quarantined fakery |
| `crr/core/settings.py` | 172–184 | `effective_global_autokick` | **needs review** — a degraded store maps to `False`, so the card renders `"global-off"`. Honest about BEHAVIOUR, but the displayed REASON is wrong: the user never turned the global switch off. The Settings modal carries the real reason; the card does not | a distinct `"degraded"` card state, or leave as-is and treat the modal as the single source of the reason | P3 — Confidence + provenance |

## Omission-pass enumeration

One row per output-producing shape introduced or changed in this delta.
`prov` and `lineage` are separate columns on purpose — a provenance string
answers *where from*, lineage answers *can I regenerate this exact output*.

| Output shape | Provenance | Confidence | Lineage | Contract | Null-expressible | Baseline |
|---|---|---|---|---|---|---|
| card `remote_control` | no | no | no (`bridge_since`/scan window not carried) | yes (v8 + enum) | **no** — unknown collapses to `off` | no |
| card `context_pressure` | **no** (window origin lost) | **no** (`bytes//4` estimate rendered as a level) | no | yes (v8 + enum) | no `unknown` level | no |
| card `autokick` | partial (3-state names global-off) | n/a | no | yes (v8 + enum) | partial (no degraded state) | no |
| card `title` / `slug` / `last_reply` | no | no | no | yes (str) | yes (`""` honest) | no |
| card `last_active` | no | no | no | yes (str) | yes (`""` honest) | no |
| `/api/discoverable` + `/api/untracked` row | **no** (cwd authoritative vs decoded collapsed) | no | no | **no** | partial | no |
| `/api/recall` payload | no | no | partial — `scanned`/`skipped` is real reproduction context | **no** | yes (empty + `skipped`) | no |
| `/api/exclusions` | yes (splits config vs managed) | n/a | no | **no** | yes | no |
| `/api/settings` | yes (stored / effective / config default / degraded) | n/a | no | **no** | yes (`None` = unset) | no |
| `exclusions.json` (stored) | no | n/a | no | **no** | yes (`[]`) | n/a |
| `settings.json` (stored) | no | n/a | no | **no** | yes (unset = `None`) | n/a |
| `bridge_kicks.json` (stored) | no | n/a | **no** — records the action, not the observation that justified it | **no** | yes (degraded fails closed) | n/a |
| adopted journal entry | partial (`sid_source=guessed`, `boot_id` sentinel) | partial | no | yes (journal v1) | n/a | no |
| `crr whoami` | yes (walks to a journaled pid) | n/a | no | no | yes (`None`) | no |
| takeover signal / `ready_to_take_over` | yes (`tail_kind`) | n/a | no | n/a (pure predicate) | yes (`""` tail) | no |

Carried advisory, not re-counted as findings: the **golden baseline** remains
`planned` (AGENTS.md:91) — logged as run-2 F19 and still the one tracked
adoption gap; and run-2 F17 (`process_probe.sleep(0.1)`) remains `proposed`.
Neither is new.

## Coverage map

Diff-scoped run; the denominator is the 26 source files this delta touches.

| Status | Files |
|---|---|
| `read` (full diff hunks) | `crr/core/`: `bridge.py`, `bridge_kicks.py`, `settings.py`, `whoami.py`, `context_pressure.py`, `takeover.py`, `exclusions.py`, `discovery.py`, `contracts.py`, `config.py`, `status.py`, `reviver.py`, `web.py` · `crr/adapters/`: `transcript_source.py` |
| `partial` | `crr/cli.py` (+1497 lines; watchdog step and adopt/discoverable paths read in full, remainder by targeted grep) · `crr/core/page.html` (+1120; JS priors and new render paths scanned, CSS not line-by-line) · `crr/core/ops.py`, `transcript.py`, `ports.py`, `diagnostics.py`, `crr/adapters/process_probe.py`, `tmux.py`, `systemd.py`, `crr/shims/crr.{bash,zsh,fish}` (diff skimmed; no finding pursued) |
| `not-read` | 22 source files unchanged in this range (`archive.py`, `journal.py`, `classifier.py`, `rescue.py`, `resume.py`, `explain.py`, `flags.py`, `launchd.py`, `scheduled_task.py`, `tab_spawn*.py`, `boot_identity.py`, `locking.py`, `state_dir.py`, `host.py`, `_proc.py`, `diagnostics_macos.py`, `diagnostics_windows.py`, …) |

```
coverage: 14/26 changed files read, 12 partial, 0 not-read within scope (54% full-read)
scope note: findings are drawn from the read set only; a partial or not-read
file with no finding is not a clean file. Files unchanged since the run-2
baseline were not re-examined. This audit does not claim completeness.
```

19 findings: 17 violations, 2 needs-review.

## Disposition — filed as `gap` issues (recursive-spine dialect)

Per `docs/tracking-dialect.md`, audit findings are `gap`-labelled issues and
work issues cite the gaps they close. Nothing from this run is deferred
without a record.

| Issue | Covers | Principle |
|---|---|---|
| [#33](https://github.com/InfiniteInsight/Claude-Remote-Rescue/issues/33) | `bridge_seen=False` conflates never-enabled with unknown | spine |
| [#34](https://github.com/InfiniteInsight/Claude-Remote-Rescue/issues/34) | `cli.py:1435` merges authoritative and lossy-decoded cwd | P3 |
| [#35](https://github.com/InfiniteInsight/Claude-Remote-Rescue/issues/35) | `bridge_kicks.json` records the action, not the observation | P8 |
| [#36](https://github.com/InfiniteInsight/Claude-Remote-Rescue/issues/36) | 8 uncontracted output shapes (5 served, 3 stored) | P7 |
| [#37](https://github.com/InfiniteInsight/Claude-Remote-Rescue/issues/37) | 6 hardcoded priors, 3 a regression of a remediated class | P5 |
| [#38](https://github.com/InfiniteInsight/Claude-Remote-Rescue/issues/38) | version-log holes + 3 docs contradicting the code | P9 / P6 |
| [#39](https://github.com/InfiniteInsight/Claude-Remote-Rescue/issues/39) | `context_pressure` against a fabricated window | P3 |
| [#40](https://github.com/InfiniteInsight/Claude-Remote-Rescue/issues/40) | the 2 needs-review findings | P4 / P3 |

Carried, not re-filed: run-2 F19 (golden baseline, `planned`) and F17
(`process_probe.sleep(0.1)`, `proposed`).
