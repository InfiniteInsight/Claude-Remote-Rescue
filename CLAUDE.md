# CLAUDE.md

Project rules for agent sessions live in **[AGENTS.md](AGENTS.md)** — read
it before writing code here.

The single rule most likely to trip you up:

> **One-way layering (machine-enforced by `.importlinter` + CI):**
> `crr.cli` → `crr.adapters` → `crr.core`. Higher imports lower; `crr.core`
> must never import `crr.adapters` or `crr.cli`. Put interfaces in
> `crr/core/ports.py`; select adapters in `crr.cli`. The DESIGN.md diagram
> arrows are runtime call flow — the *inverse* of the import direction.

Runtime dependencies stay at zero (stdlib-only web server; dependency-free
shims). New behavior is test-first. See AGENTS.md for the rest.
