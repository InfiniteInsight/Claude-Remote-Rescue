# plumb-line remediation record

```
remediation-format: v1
source-report:       claude-code-autoresume/plumb-line-audit.md
source-report-format: v3
principles-revision: 1
date:                2026-07-23
commit:              3b64bd2 (before remediation; applied as 6e4900b)
```

Scope: the five findings the audit's disposition carried to this repo, applied
as design-doc requirements (this repo is docs-only; Phase 0/1 implement them).

| Finding | Path | Class | Action | Change summary |
| ------- | ---- | ----- | ------ | -------------- |
| Guessed sid laundered (P3 — Confidence + provenance) | `DESIGN.md` schema + wrapper | mechanical | applied-mechanical | `sid_source: injected\|guessed\|verified` in schema v1; re-verification promoted from "TBD" to requirement; 2026-07-21 incident cited |
| Hardcoded priors (P5 — Injectable priors) | `DESIGN.md` config | mechanical | applied-mechanical | Enumerated prior list becomes the config floor; new priors join at introduction time |
| Uncontracted outputs (P7 — Contracted outputs) | `DESIGN.md` web | mechanical | applied-mechanical | API payloads get version constant + canonical keys + importable validator |
| Unenforced layering (P2 — One-way layering) | `DESIGN.md` architecture, `ROADMAP.md` Phase 0 | mechanical | applied-mechanical | Ruleset file + CI import-linter required day one; composition root = CLI entry |
| Invisible config defaults (P3 — Confidence + provenance) | `DESIGN.md` config | mechanical | applied-mechanical | `crr config --effective` with per-key `configured\|default` origin |

Proposed (not applied): none.
Blocked / applied-conservative: none — all fixes were requirement-encoding;
no epistemic value had to be invented.

Verification: document-consistency checks only (5 audit-requirement markers,
3 `sid_source` references, clean 2-file diff); the repo has no runnable
enforcement yet — mandating that tooling (Phase 0) is itself this run's
P2 — One-way layering fix.

Companion fixes applied directly in the source repo (commit b5c3aa1 there):
archive lineage (P8 — State-first lineage), `zombie_strikes` config
(P5 — Injectable priors), linkstatus + PRODUCT.md maturity corrections
(P6 — Maturity vocabulary).
