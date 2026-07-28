# Packaging artifacts

Repo-side packaging files for `claude-remote-rescue` v0.1.0. Publishing
itself (cutting the `v0.1.0` tag, filling in checksums, submitting to
Homebrew core / the AUR, announcing) is human-gated and belongs in
`docs/RUNBOOK-cutover-and-release.md` (not yet written as of this task;
this directory only holds the files that describe how to build the
package, not the runbook that executes the publish).

## Files

- **`homebrew/claude-remote-rescue.rb`** — a Homebrew formula. Builds from
  the GitHub release tarball for tag `v0.1.0` via
  `Language::Python::Virtualenv`. No `resource` blocks: the project has zero
  runtime dependencies, so there is nothing to vendor. `test do` runs
  `crr --help`.
- **`aur/PKGBUILD`** — an Arch Linux package build script. Same release
  tarball, standard Python wheel build/install
  (`python -m build` → `python -m installer`), MIT license installed to
  `/usr/share/licenses/$pkgname/`.
- **`aur/.SRCINFO`** — the AUR's generated-metadata sidecar for `PKGBUILD`.
  **Hand-written here** (no `makepkg` available on the authoring machine) —
  see the "Validation the publisher must run" section below for how to
  regenerate it for real before pushing to the AUR.

## Placeholder checksums

Both `homebrew/claude-remote-rescue.rb` and `aur/PKGBUILD` (and its
`.SRCINFO` mirror) reference the `v0.1.0` GitHub release tarball, which does
not exist yet — no tag has been cut. Each file carries an obviously-fake
sha256 placeholder:

- Homebrew: `sha256 "PLACEHOLDER_FILL_AFTER_TAGGING_SEE_packaging_README"`
- AUR: `sha256sums=('PLACEHOLDER_FILL_AFTER_TAGGING')`

**After the `v0.1.0` tag is pushed**, the publisher fills both in with the
real checksum of the release tarball:

```sh
curl -L <tarball-url> | shasum -a 256   # on Linux: sha256sum
```

Paste the resulting hex digest into both `sha256 "..."` in
`homebrew/claude-remote-rescue.rb` and `sha256sums=('...')` in
`aur/PKGBUILD`, then regenerate `aur/.SRCINFO` (see below) so it stays
consistent with the PKGBUILD.

## Validation done on this machine

This is an Ubuntu/WSL development box with no `brew`, `ruby`, or `makepkg`
installed, so the two package managers' own tooling could not be run here.
What *was* checked:

- `bash -n packaging/aur/PKGBUILD` — the PKGBUILD parses as valid shell
  (catches syntax errors in `build()`/`package()`, not Arch-specific
  correctness).
- A local wheel build proving the project's metadata and package-data
  declarations actually produce an installable wheel with the shims and
  `page.html` included:
  ```sh
  # --no-build-isolation needs setuptools present in the target venv
  # first (pip's own build isolation is what normally supplies it):
  .venv/bin/pip install setuptools
  .venv/bin/pip wheel --no-build-isolation --no-deps -w /tmp/wheeltest .
  .venv/bin/python -m zipfile -l /tmp/wheeltest/claude_remote_rescue-0.1.0-py3-none-any.whl
  ```
  Without that first step, `pip wheel --no-build-isolation` fails with
  `BackendUnavailable: Cannot import 'setuptools.build_meta'`.
  This is the same mechanism both the Homebrew formula
  (`virtualenv_install_with_resources`, which does a `pip install` of the
  sdist/tarball) and the AUR `PKGBUILD` (`python -m build --wheel` +
  `python -m installer`) rely on to produce a correct installation, so a
  clean wheel build here is meaningful evidence for both.
- `ruby -c` was **not** run: no `ruby` binary is present on this machine.

None of this substitutes for the real tools. In particular, **no
`brew audit`, `brew install --build-from-source`, or `makepkg` run has
happened anywhere for these files.**

## Validation the publisher must run

Before submitting either package, after the `v0.1.0` tag exists and the
checksums above are filled in:

- **Homebrew:**
  ```sh
  brew audit --new --strict packaging/homebrew/claude-remote-rescue.rb
  brew install --build-from-source packaging/homebrew/claude-remote-rescue.rb
  brew test packaging/homebrew/claude-remote-rescue.rb
  ```
  Note: homebrew-core's `brew audit --new` applies a repository-notability gate (~75 stars); if the repo doesn't clear it yet, submit to a personal tap (e.g. `InfiniteInsight/homebrew-tap`) instead — the formula file is identical either way.
- **AUR (on an Arch machine or container):**
  ```sh
  makepkg -si          # build + install, verifies PKGBUILD end-to-end
  makepkg --printsrcinfo > .SRCINFO   # regenerate .SRCINFO for real; diff
                                       # against the hand-written version
                                       # above and commit the regenerated one
  ```

Only after both pass should the packages be submitted (`homebrew-core` PR,
`git push` to the AUR's `ssh://aur@aur.archlinux.org/claude-remote-rescue.git`).
