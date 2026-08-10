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
