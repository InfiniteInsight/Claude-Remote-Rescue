"""tmux adapter (implements crr.core.ports.TmuxSpawner).

Thin subprocess wrapper. The command *shapes* are pure builders so they
can be unit-tested without a running tmux; the one method that needs a
real server (``new_detached_session``) is covered by an integration test
gated on tmux being installed.

``new_detached_session`` passes argv word-form after ``--`` so tmux execs
the target directly instead of wrapping it in the login shell, which would
re-source the shim and double-register the session ([lesson: word-form
exec]).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Sequence


def _list_sessions_cmd() -> list[str]:
    return ["tmux", "list-sessions", "-F", "#{session_name}"]


def _new_session_cmd(name: str, cwd: str, argv: Sequence[str]) -> list[str]:
    # `--` terminates option parsing; the rest is exec'd word-form.
    return ["tmux", "new-session", "-d", "-s", name, "-c", cwd, "--", *argv]


def _kill_session_cmd(name: str) -> list[str]:
    return ["tmux", "kill-session", "-t", name]


def _parse_sessions(stdout: str) -> set[str]:
    return {line for line in stdout.splitlines() if line}


class RealTmux:
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def list_sessions(self) -> set[str]:
        try:
            result = subprocess.run(
                _list_sessions_cmd(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return set()
        # A non-zero exit means "no server running" (hence no sessions) far
        # more often than a real error; treat it as empty rather than raising.
        if result.returncode != 0:
            return set()
        return _parse_sessions(result.stdout)

    def new_detached_session(self, name: str, cwd: str, argv: Sequence[str]) -> None:
        subprocess.run(
            _new_session_cmd(name, cwd, argv),
            capture_output=True, text=True, timeout=self._timeout, check=True,
        )

    def kill_session(self, name: str) -> None:
        subprocess.run(
            _kill_session_cmd(name),
            capture_output=True, text=True, timeout=self._timeout, check=True,
        )
