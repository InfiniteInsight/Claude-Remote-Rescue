# Contributing

Thanks for your interest in Claude-Remote-Rescue. The project has a small
number of hard rules that keep it maintainable and honest; please read them
before opening a PR. The authoritative, longer version is
[AGENTS.md](AGENTS.md) — this is the summary.

## Ground rules

1. **One-way layering (machine-enforced).** `crr.cli` → `crr.adapters` →
   `crr.core`. Higher layers import lower ones; `crr.core` must **never**
   import `crr.adapters` or `crr.cli`. Interfaces (ports) live in
   `crr/core/ports.py`; adapters are selected in `crr.cli` (the one
   composition root). CI runs `lint-imports` and fails on any upward import.

2. **Zero runtime dependencies.** The web server is stdlib-only and the shell
   shims are dependency-free. Adding a runtime dependency to
   `pyproject.toml` is a design regression, not a convenience. (Dev/test
   tools under `[project.optional-dependencies].dev` are fine.)

3. **Test-first (TDD).** New behavior and bug fixes start with a failing
   test. Business logic lives in `crr/core` and is tested with fakes/fixtures
   (no OS, no network); OS/subprocess code lives in `crr/adapters` and is
   tested with platform-gated tests that skip cleanly elsewhere.

4. **Contracts are versioned.** The `/api/*` payloads and the stored journal
   have version constants and validators in `crr/core/contracts.py`. If you
   change a shape, bump its version and update the validator.

5. **Judgment-call constants are config, not magic numbers.** Any new timing
   or threshold value becomes a named prior in `crr/core/config.py` with a
   documented default, so it shows up in `crr config --effective`.

6. **`page.html` changes bump `PAGE_VERSION`.** See below.

## Running the checks locally

```sh
python -m pip install -e '.[dev]'
pytest -q            # unit + shim + web tests
lint-imports         # the layering contract
```

Platform adapter tests (macOS, shells) skip when their platform/tools are
absent; CI runs the full matrix (ubuntu + macOS, Python 3.11/3.12) plus a
`node --check` gate on the dashboard's inline JavaScript.

## The `PAGE_VERSION` discipline

The dashboard self-heals a stale/broken cached page via `PAGE_VERSION`
(`crr/core/web.py`) + `/api/version` polling. **Whenever you change
`crr/core/page.html` after a release, bump `PAGE_VERSION`.** If you don't, a
client holding a cached page will never learn it should reload — the exact
failure the self-heal exists to prevent. A test (`node --check`) guarantees
the served script parses; only the version bump makes clients pick it up.

## Commit / PR conventions

- One focused change per PR; keep the layering green.
- Conventional-style commit subjects (`feat(...)`, `fix(...)`, `test(...)`,
  `docs(...)`) are appreciated.
- Describe how you verified the change, and call out anything you could *not*
  verify (a platform you don't have, hardware you couldn't test) rather than
  implying full coverage.

Not affiliated with Anthropic. MIT licensed.
