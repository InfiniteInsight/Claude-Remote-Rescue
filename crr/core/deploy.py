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


def resolve_repo(
    *,
    explicit: str | None,
    explicit_is_checkout: bool,
    repo_root: Path,
    repo_root_is_checkout: bool,
    marker_repo: str | None,
    marker_repo_is_checkout: bool,
) -> Path | None:
    """Which repo `crr deploy` should build from.

    `crr deploy` re-invoked through the symlink it just created has
    `__file__` inside a venv's site-packages, not a checkout — deploy
    breaking its own re-invocation path is why a deployed snapshot sat 34
    commits stale before anyone noticed. Precedence: an explicit ``--repo``
    always wins over guessing, but only when it actually is a checkout — a
    typo'd path must refuse on itself, never silently fall through to a
    DIFFERENT repo the operator didn't name. Otherwise, the checkout crr
    was imported from, if it still looks like one (the common case: running
    from source). Otherwise, the checkout recorded the last time someone
    deployed successfully from a real source tree (the deployed-copy case).
    ``None`` means nothing usable was found — the caller refuses loudly
    rather than building from a guess.
    """
    if explicit:
        return Path(explicit) if explicit_is_checkout else None
    if repo_root_is_checkout:
        return repo_root
    if marker_repo and marker_repo_is_checkout:
        return Path(marker_repo)
    return None


def no_checkout_refusal(explicit: str | None) -> str:
    """Why deploy has nothing to build from, phrased so the next step is
    obvious.

    The message this replaces ("is this a git checkout?") sent the
    reporter of the underlying bug down the wrong path: it looked like a
    question about their tree, not a statement that deploy needs a
    checkout it currently cannot find. This one names both ways out.
    """
    if explicit:
        return (f"--repo {explicit} is not a git checkout — pass the path "
                "to a real source checkout")
    return ("could not find a git checkout to deploy from — run `crr "
            "deploy` from the source checkout, or pass --repo PATH")


def deploy_status(
    *,
    deployed_sha: str | None,
    repo_known: bool,
    head_sha: str | None,
    is_ancestor: bool | None,
    commits_behind: int | None,
) -> tuple[bool | None, str]:
    """The (``_check`` ok, detail) doctor renders for the deployed snapshot
    vs. the source repo's HEAD.

    Every probe input already degraded to ``None`` rather than a guess
    (``crr.adapters.deploy``'s probes); this function only decides how to
    say it. An unmeasurable comparison is an informational caveat, never a
    claimed match or claimed drift — inventing either would be worse than
    silence (spine: null-result expressibility). ``repo_known`` carries a
    fact ``head_sha is None`` alone can't: "no checkout could be found" and
    "a checkout was found but reading its HEAD failed" both leave
    ``head_sha`` at ``None``, but they are different true statements —
    conflating them into one caveat line would make the "checkout unknown"
    wording a false claim in the second case.
    """
    if deployed_sha is None:
        return (True, "nothing deployed — services run the working tree")
    if head_sha is None:
        if not repo_known:
            return (True, f"deployed {deployed_sha[:7]} — source checkout "
                           "unknown, cannot compare to HEAD")
        return (True, f"deployed {deployed_sha[:7]} — checkout found but its "
                       "HEAD could not be read, cannot compare")
    if deployed_sha == head_sha:
        return (True, f"deployed {deployed_sha[:7]} — up to date")
    if is_ancestor:
        # Ancestry is a confirmed, real fact even when the exact count
        # isn't — reporting "cannot be compared" here would understate a
        # known-true staleness just because the second of two probes
        # failed. Never claims a false PRECISE count, only omits it.
        if commits_behind is not None:
            return (False, f"deployed {deployed_sha[:7]} — {commits_behind} "
                            f"commit(s) behind {head_sha[:7]}; run crr deploy")
        return (False, f"deployed {deployed_sha[:7]} — behind {head_sha[:7]} "
                       "(commit count unknown); run crr deploy")
    return (True, f"deployed {deployed_sha[:7]} — cannot be compared to "
                   f"HEAD ({head_sha[:7]})")


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
