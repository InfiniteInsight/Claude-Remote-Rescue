# Packaging artifacts (task #9, repo-file portion)

**Date:** 2026-07-27 · **Status:** approved design (resolved autonomously
per the handoff). Publishing itself (tag, submissions, announcement) is
human-gated and lives in `docs/RUNBOOK-cutover-and-release.md` — this task
only authors repo files.

## Decisions

1. **Version stays 0.1.0.** No tag or release has ever been cut, so 0.1.0
   is the *first* release and already contains everything on main. Bumping
   would fabricate a release history that never happened.
2. **Layout:** `packaging/homebrew/claude-remote-rescue.rb`,
   `packaging/aur/PKGBUILD`, `packaging/aur/.SRCINFO`,
   `packaging/README.md` (what each file is, how to validate, what the
   publisher must fill in), plus top-level `CHANGELOG.md`.
3. **Homebrew formula:** builds from the GitHub release tarball
   (`v0.1.0`), `depends_on "python@3.12"`, installs via
   `Language::Python::Virtualenv` with **no resources** (zero runtime
   deps is a project guarantee). The tarball `sha256` cannot exist before
   the tag does → a clearly marked `PLACEHOLDER_` value, called out in
   packaging/README.md and the runbook. `test do` runs `crr --help`.
4. **AUR:** `pkgname=claude-remote-rescue` (not `crr` — too collision-prone
   for a first release), standard Arch Python packaging
   (`python-build`/`python-installer`/`python-wheel`/`python-setuptools`
   makedepends, `python` dep), source = the same release tarball with a
   placeholder checksum, MIT license installed to the standard path.
   `.SRCINFO` is hand-written (no `makepkg` on this Ubuntu box) and says so.
5. **CHANGELOG.md:** Keep-a-Changelog shape, single `## [0.1.0] —
   unreleased` section (honest: pending the first public tag) enumerating
   the feature set, including the day-one behavior note that any nonzero
   claude exit (including a SIGINT quit) now triggers the shim's resume
   offer.
6. **Offline validation** (all that is possible without brew/makepkg):
   `ruby -c` if ruby exists, `bash -n` on the PKGBUILD, and a local wheel
   build (`pip wheel --no-build-isolation`) proving the sdist metadata is
   packagable. `brew audit`/`makepkg` runs are listed in the runbook for
   the publisher.
