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
