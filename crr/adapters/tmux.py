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


def _session_pid_cmd(name: str) -> list[str]:
    return ["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"]


def _kill_session_cmd(name: str) -> list[str]:
    return ["tmux", "kill-session", "-t", name]


def _parse_sessions(stdout: str) -> set[str]:
    return {line for line in stdout.splitlines() if line}


class RealTmux:
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def session_pid(self, name: str) -> int | None:
        """The pid running in ``name``'s first pane, or None if unknown.

        A revived session's pane pid IS the claude process (the reviver
        spawns `tmux new-session -- claude ...` with no shell in between),
        which is what lets the journal be re-keyed onto the live process
        (#58). None means "could not determine" and is never guessed at:
        re-keying an entry onto a pid we did not observe would point every
        pid-keyed op at the wrong process.
        """
        try:
            result = subprocess.run(
                _session_pid_cmd(name),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        first = result.stdout.strip().split("\n", 1)[0].strip()
        try:
            return int(first)
        except ValueError:
            return None

    def list_sessions(self) -> set[str] | None:
        """Return the live tmux session names, or None if that could not be
        determined (audit F16 — tri-state, spine: null-result expressibility).

        A timeout or OSError is an unknown state, not an empty one — None.
        A non-zero exit is genuinely empty (``set()``) only when tmux's own
        stderr says there is no server to have sessions: "no server running"
        (a server existed and stopped) or "error connecting to ... (No such
        file or directory)" (a server was never started on this socket —
        the common case for a fresh TMUX_TMPDIR, e.g. the RealTmux
        integration test below). [inspect-and-decide, measured on tmux 3.4]
        Any OTHER non-zero exit (permission error, corrupted socket, ...) is
        an unknown state too — never collapse it into "confirmed no
        sessions", or a transient query failure could accumulate a revive
        strike or a refusal-worthy op against a session that may still be
        alive.
        """
        try:
            result = subprocess.run(
                _list_sessions_cmd(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            stderr = (result.stderr or "").lower()
            if "no server running" in stderr or "error connecting to" in stderr:
                return set()
            return None
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
