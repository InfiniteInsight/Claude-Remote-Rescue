"""Deploy I/O — git facts and building the services' frozen copy (#61).

Decisions live in ``crr.core.deploy``; this module only runs commands and
reports what happened. Every probe degrades to None ("could not tell")
rather than a confident wrong answer — the caller refuses on unknown.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run_git(repo: Path, *args: str, timeout: float) -> subprocess.CompletedProcess | None:
    """Run ``git -C repo *args``, or None if it could not even be run (git
    missing, timed out). The one place the command line is built and
    ``SubprocessError``/``OSError`` is caught; ``_git`` and ``is_ancestor``
    below both read the result, ``_git`` for stdout-on-success,
    ``is_ancestor`` for the returncode itself (0/1/anything-else are three
    different answers, not success-or-None).
    """
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def _git(repo: Path, *args: str, timeout: float) -> str | None:
    result = _run_git(repo, *args, timeout=timeout)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def head_sha(repo: Path, timeout: float = 5) -> str | None:
    """The repo's current commit, or None if that cannot be determined."""
    return _git(repo, "rev-parse", "HEAD", timeout=timeout) or None


def is_dirty(repo: Path, timeout: float = 5) -> bool | None:
    """True if the working tree has uncommitted tracked changes.

    None means "could not tell" — not a git checkout, git missing, or the
    probe failed. Untracked files are deliberately ignored: a scratch file
    beside the source does not change what gets installed.
    """
    out = _git(repo, "status", "--porcelain", "--untracked-files=no", timeout=timeout)
    if out is None:
        return None
    return bool(out.strip())


def build(app_dir: Path, repo: Path, sha: str | None, timeout: float = 600) -> str | None:
    """Install ``repo`` into a fresh venv at ``app_dir``. Returns an error or None.

    Non-editable on purpose: the point is a copy that does not move when the
    working tree does. ``--no-deps`` holds the project's zero-runtime-deps
    rule — anything it pulled in would be a dependency crr does not have.
    """
    try:
        made = subprocess.run(
            ["python3", "-m", "venv", str(app_dir)],
            capture_output=True, text=True, timeout=timeout,
        )
        if made.returncode != 0:
            return f"venv creation failed: {made.stderr.strip() or made.stdout.strip()}"
        installed = subprocess.run(
            [str(app_dir / "bin" / "pip"), "install", "--no-deps", "--upgrade", str(repo)],
            capture_output=True, text=True, timeout=timeout,
        )
        if installed.returncode != 0:
            return f"install failed: {installed.stderr.strip() or installed.stdout.strip()}"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"deploy failed: {exc}"
    return None


def write_marker(path: Path, sha: str | None, at: str, repo: str | None = None) -> None:
    """Record what was deployed, so drift can be reported later.

    ``repo`` is the source checkout deploy built from — recorded so a
    LATER deploy, re-invoked through the very symlink this one creates,
    can find its way back to a real checkout even though `__file__` by
    then points into the frozen copy's site-packages. Optional and
    omitted when unknown, so older markers (written before this field
    existed) keep round-tripping without it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, str | None] = {"sha": sha, "deployed_at": at}
    if repo:
        data["repo"] = repo
    path.write_text(json.dumps(data), encoding="utf-8")


def read_marker(path: Path) -> str | None:
    """The deployed commit, or None if nothing is deployed / it is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) and sha else None


def read_marker_repo(path: Path) -> str | None:
    """The source checkout recorded at deploy time, or None if there isn't
    one (an older marker predating this field) or the marker is unreadable.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repo = data.get("repo") if isinstance(data, dict) else None
    return repo if isinstance(repo, str) and repo else None


def is_checkout(repo: Path) -> bool:
    """Whether ``repo`` looks like a git checkout: a ``.git`` entry (a
    directory for an ordinary clone, a file for a worktree). A plain
    filesystem check — cheap enough to try before running git at all, and
    the input to deciding WHICH candidate path deploy should even attempt
    to probe with git.
    """
    try:
        return (Path(repo) / ".git").exists()
    except OSError:
        return False


def is_ancestor(repo: Path, ancestor: str, descendant: str, timeout: float = 5) -> bool | None:
    """Whether ``ancestor`` is in ``descendant``'s history, or None if that
    could not be determined — the sha is unknown to this repo (rebased,
    squashed, gone), git is missing, or the probe failed/timed out. Never
    guessed True: a wrong "behind" claim sends someone chasing a deploy for
    a commit that was never really there.
    """
    result = _run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant, timeout=timeout)
    if result is None:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None  # e.g. "not a valid object name" — unknown, not "no"


def commits_behind(repo: Path, deployed_sha: str, head_sha: str, timeout: float = 5) -> int | None:
    """How many commits ``head_sha`` has that ``deployed_sha`` does not, or
    None on any failure. Only meaningful once the caller has confirmed
    ``deployed_sha`` is an ancestor of ``head_sha`` via ``is_ancestor()`` —
    on a diverged pair this counts the whole unshared side, not a
    straight-line "behind" distance.
    """
    out = _git(repo, "rev-list", "--count", f"{deployed_sha}..{head_sha}", timeout=timeout)
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def restart_service(timeout: float = 30) -> str | None:
    """Restart crr-web.service so it picks up the deployed code.

    Returns an error message on failure, or None on success.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "crr-web.service"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return f"restart failed: {result.stderr.strip() or result.stdout.strip()}"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"restart failed: {exc}"
    return None


def ensure_link(link: Path, target: Path) -> str | None:
    """Point ``link`` at ``target``. Returns an error message, or None.

    Replaces an existing symlink (a re-deploy must be able to re-point a
    stale one) but never a regular file — the caller checks that first.
    """
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    except OSError as exc:
        return f"could not link {link} -> {target}: {exc}"
    return None
