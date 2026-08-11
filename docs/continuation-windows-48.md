# Continuation prompt — fix the 48 Windows test failures (#72)

Copy everything below the line into a fresh session.

---

Work on **issue #72** in `/home/evan/projects/Claude-Remote-Rescue`: 48 tests
fail on Windows now that the suite can run there. Read the issue first
(`gh issue view 72`) — it has the category breakdown and the CI run id.

## Where things stand

`main` is at `55fd0fb`, all CI green (ubuntu + macOS + page-js). Windows is
**not** in the CI matrix — deliberately, with the reason written into
`.github/workflows/ci.yml`. crr *imports and runs* on Windows as of #70; what
remains is the suite.

To see the failures you must add `windows-latest` back to the matrix on your
branch and push. That is the only way to observe them — there is no Windows
machine locally, and **guessing at platform behaviour without the platform is
the specific mistake that cost #65 three CI rounds.** Make the failing test
report its evidence and push, rather than reasoning about what Windows
probably does.

## Known categories (from the #72 survey)

- **POSIX syscalls in tests** — `test_proc.py`'s two zombie tests use
  `os.setsid`. The older process-group tests in the same file already carry
  `@pytest.mark.skipif(os.name != "posix")`; these need the same.
- **PATH separator** — `test_systemd.py` / `test_launchd.py`
  `resolve_service_path` build and split PATH with `:`. Windows uses `;`
  (`os.pathsep`). The one *product* instance was `deploy.path_warning`, fixed
  in #70.
- **Line endings** — `test_page_version_guard` sha256s `page.html`. A CRLF
  checkout changes the bytes so the pin cannot match. Likely wants
  `.gitattributes` with `*.html text eol=lf`, or a normalised read.
- **Unclassified, 36 of them** — all in `test_cli.py`, plus `test_web.py` (2),
  `test_payload_contracts.py`, `test_cwd_provenance.py`, `test_adapters.py`.
  Nobody has read these yet. Do not assume they are all test-side; #70 turned
  up a real product bug (`path_warning`) hiding among what looked like
  test-only noise.

## The rule that matters here

Distinguish **"the test assumes POSIX"** from **"crr is broken on Windows"**
one failure at a time. A test guarded with `skipif` is a claim that the
behaviour does not apply on that platform; if it *does* apply and crr gets it
wrong, the guard hides a defect. When you cannot tell without the platform,
say so by name — `test_shims.py::_skip_fish_pty_on_macos` is the pattern:
skip narrowly, state what is unverified, point at the issue.

## Definition of done

`windows-latest` back in the matrix and **green**, with every remaining skip
carrying a stated reason. Then close #72. If some failures turn out to need
real Windows behaviour crr does not implement, split those into their own
issue rather than leaving a skip that quietly means "unsupported".

## Project conventions

Read `AGENTS.md` first. Branch `fix/72-<slug>`; PR says `Closes #72`.
Test-first. Run before committing:

```
.venv/bin/python -m pytest tests/ -q
.venv/bin/lint-imports
```

The pre-commit hook runs both and blocks on failure. `crr` on PATH is the
**deployed** copy, not the working tree — use `.venv/bin/crr` when testing
your changes, and `crr deploy` when you want them live (#61).
