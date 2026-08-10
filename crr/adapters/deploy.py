"""Deploy I/O — git facts and building the services' frozen copy (#61).

Decisions live in ``crr.core.deploy``; this module only runs commands and
reports what happened. Every probe degrades to None ("could not tell")
rather than a confident wrong answer — the caller refuses on unknown.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str, timeout: float) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
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


def write_marker(path: Path, sha: str | None, at: str) -> None:
    """Record what was deployed, so drift can be reported later."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sha": sha, "deployed_at": at}), encoding="utf-8")


def read_marker(path: Path) -> str | None:
    """The deployed commit, or None if nothing is deployed / it is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) and sha else None


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
