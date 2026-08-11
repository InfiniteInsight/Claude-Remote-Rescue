"""Deploy decisions — which code the *services* are allowed to run.

The watchdog and dashboard were wired straight at the development working
tree through an editable install, so every save reached a process that
mutates real session state within one timer interval — no commit, no test
run, no opt-in (#61). Observed: a half-finished change was applied to live
sessions before it was committed.

Refusing to run on a dirty tree is not the answer: the watchdog exists to
keep conversations alive, and going silent while someone edits is the one
failure it must not have. So the services run from a *deployed copy* that
only moves when someone says so, and editing the tree is free.

Pure: every function here is a decision over values. Running git and pip
lives in ``crr.adapters.deploy``.
"""

from __future__ import annotations

import os
from pathlib import Path

# Under XDG_STATE_HOME/crr, beside the journal the services already read.
APP_DIRNAME = "app"
MARKER_NAME = "deployed.json"


def app_dir(state_dir: Path) -> Path:
    """Where the services' frozen copy lives."""
    return Path(state_dir) / APP_DIRNAME


def marker_path(state_dir: Path) -> Path:
    """Where the deployed commit is recorded."""
    return app_dir(state_dir) / MARKER_NAME


def deployed_bin(state_dir: Path) -> Path:
    """The ``crr`` the service units should point at."""
    return app_dir(state_dir) / "bin" / "crr"


def refusal(*, dirty: bool | None, force: bool) -> str | None:
    """Why this deploy must not proceed, or None to go ahead.

    A dirty tree is refused because the whole point is that services run
    reviewed code: deploying uncommitted work would rebuild the hazard with
    extra steps. ``force`` is the deliberate override, and unknown dirtiness
    is refused too — "could not tell" is not "clean" (spine: null-result
    expressibility), and guessing here puts unreviewed code on live state.
    """
    if force:
        return None
    if dirty is None:
        return ("cannot tell whether the working tree is clean (is this a git "
                "checkout?) — refusing to deploy code that may be uncommitted; "
                "use --force to override")
    if dirty:
        return ("working tree has uncommitted changes — commit them first so the "
                "services run reviewed code, or use --force to override")
    return None


def drift(deployed_sha: str | None, head_sha: str | None) -> str | None:
    """A one-line warning when the services are not running HEAD, else None.

    Not an error: running an older deploy is a legitimate state (that is the
    point). It is only worth saying out loud, because the difference between
    "my fix is live" and "my fix is committed" is invisible otherwise.
    """
    if deployed_sha is None:
        return "no deployed copy — services are running the working tree directly"
    if head_sha is None or deployed_sha == head_sha:
        return None
    return (f"services are running {deployed_sha[:7]}, working tree is at "
            f"{head_sha[:7]} — run `crr deploy` to update them")


# The conventional per-user bin dir. A deploy puts `crr` on PATH here so the
# command works from anywhere, pointing at the same reviewed copy the
# services run rather than at whatever venv happens to be active.
LINK_DIRNAME = Path(".local") / "bin"


def link_path(home: Path) -> Path:
    """Where `crr` should be linked so it is callable from anywhere."""
    return Path(home) / LINK_DIRNAME / "crr"


def link_refusal(link: Path) -> str | None:
    """Why the link must not be written, or None to go ahead.

    A real file there is somebody else's install (a pip --user script, a
    hand-written wrapper). Replacing a symlink crr owns is routine;
    clobbering a regular file is not ours to do.
    """
    if link.is_symlink():
        return None
    if link.exists():
        return (f"{link} exists and is not a symlink — leaving it alone; "
                "remove it first if you want `crr` to point at the deployed copy")
    return None


def path_warning(path_env: str, link: Path, sep: str | None = None) -> str | None:
    """Warn when the link lands somewhere PATH will not find, else None.

    ``sep`` defaults to this platform's PATH separator — ``:`` on POSIX,
    ``;`` on Windows. Splitting on a hardcoded ``:`` made every Windows PATH
    parse as one entry, so the warning fired for a directory that was on it
    (#70's survey; written today, caught by the Windows CI job the same day).
    """
    separator = os.pathsep if sep is None else sep
    def _norm(value: str) -> str:
        # normcase folds Windows case and slashes; normpath drops trailing
        # separators. Both are identity on POSIX. Without this, a PATH entry
        # and the link's parent could name the same directory and compare
        # unequal purely on spelling.
        return os.path.normcase(os.path.normpath(value))

    parent = _norm(str(link.parent))
    entries = [_norm(e) for e in (path_env or "").split(separator) if e]
    if parent in entries:
        return None
    return (f"{parent} is not on PATH — `crr` will not be found by name until "
            "you add it")
