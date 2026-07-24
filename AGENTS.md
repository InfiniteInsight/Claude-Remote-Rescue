# Agent ruleset — Claude-Remote-Rescue

This file declares the project's hard constraints for AI-agent sessions
(audit P2 — One-way layering, made machine-enforced). Claude Code reads
`CLAUDE.md`, which points here; other harnesses read this file directly.

## The one-way layering rule (non-negotiable)

Import direction is enforced by `.importlinter` and CI (`lint-imports`).
Higher may import lower; lower importing higher fails the build.

```
crr.cli        composition root — the ONLY module that may import both
   │           adapters and core.
   ▼
crr.adapters   platform adapters — may import crr.core, never crr.cli.
   │
   ▼
crr.core       pure core (stdlib only) — imports neither adapters nor cli.
```

- Do **not** add `import crr.adapters` inside `crr/core/**`. Put the
  interface (a Protocol/ABC) in `crr/core/ports.py` and have the adapter
  implement it. Adapter *selection* happens in `crr.cli`.
- The DESIGN.md diagram arrows (core → adapters) are runtime **call**
  flow — the inverse of the import rule. Don't "fix" `.importlinter` to
  match the diagram.

## Dependencies

- **Runtime deps stay at zero.** The web server is stdlib `http` only;
  the shell shims are dependency-free (they shell out to `crr`). Adding a
  runtime dependency to `pyproject.toml` is a design regression — raise it
  as a design change, don't slip it in.
- Dev deps (pytest, import-linter) live under `[project.optional-dependencies].dev`.

## Testing discipline

- New behavior is test-first (TDD). Watch the test fail before
  implementing.
- Contract shapes (`crr/core/contracts.py`) are versioned. Change a
  stored/served shape → bump its version constant → update its validator
  and tests. An unversioned shape change is the exact laundering the audit
  flagged.
- Every `<script>` block served by the web page must pass `node --check`
  in CI (a served page is not verifiable by curl).

## Provenance & honesty (the plumb-line principles)

- Confidence travels with data. `sid_source` (`injected|guessed|verified`)
  must survive from journal → `status --json` → dashboard; never present a
  `guessed` sid as truth.
- Every judgment-call constant is named config with a versioned default,
  never a magic number in logic. `crr config --effective` must report each
  key's origin (`configured` vs `default`).
- Destructive operations gate on the classifier, never bare
  pid-existence (recycled pids kill bystanders).

See DESIGN.md for the full rationale and the `[lesson]` markers that each
rule was paid for.
