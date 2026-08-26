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


def _attached_sessions_cmd() -> list[str]:
    # -f keeps only sessions whose #{session_attached} is truthy (a client is
    # attached — the user has reopened it); -F prints just the name. A parked
    # (detached) session has session_attached == 0 and is filtered out.
    return ["tmux", "list-sessions", "-f", "#{session_attached}", "-F", "#{session_name}"]


def _new_session_cmd(name: str, cwd: str, argv: Sequence[str]) -> list[str]:
    # `--` terminates option parsing; the rest is exec'd word-form.
    return ["tmux", "new-session", "-d", "-s", name, "-c", cwd, "--", *argv]


def _session_pid_cmd(name: str) -> list[str]:
    return ["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"]


def _kill_session_cmd(name: str) -> list[str]:
    return ["tmux", "kill-session", "-t", name]


def _current_session_cmd() -> list[str]:
    return ["tmux", "display-message", "-p", "#S"]


def _capture_pane_cmd(name: str) -> list[str]:
    # -J joins wrapped lines: without it, a long OAuth URL wraps at the
    # pane's column width (the default 80 columns wraps a ~200-char URL),
    # so the caller's regex would capture a truncated, invalid URL instead
    # of the whole line.
    return ["tmux", "capture-pane", "-t", name, "-p", "-J"]


def _send_keys_cmd(name: str, text: str) -> list[str]:
    return ["tmux", "send-keys", "-t", name, text, "Enter"]


def _parse_sessions(stdout: str) -> set[str]:
    return {line for line in stdout.splitlines() if line}


def _empty_or_unknown(stderr: str) -> set[str] | None:
    """A nonzero tmux list-sessions exit: genuinely empty (``set()``) only
    when tmux's own stderr says there is no server, else unknown (``None``).

    Shared by ``list_sessions`` and ``attached_sessions`` so the tri-state
    boundary (audit F16 — never collapse "can't tell" into "confirmed
    empty") stays identical for both queries.
    """
    s = (stderr or "").lower()
    if "no server running" in s or "error connecting to" in s:
        return set()
    return None


class RealTmux:
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def session_pid(self, name: str) -> int | None:
        """The pid running in ``name``'s first pane, or None if unknown.

        A revived session's pane pid is the process the reviver launched —
        now the minimal ``sh`` exit-hook wrapper that hosts claude as its
        child ([/exit revival 2026-08-24]; legacy sessions from before that change ran ``claude``
        directly, and both re-key identically). That pane pid is what the
        journal is re-keyed onto (#58), so kick/classify act on a real live
        process. None means "could not determine" and is never guessed at:
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
            return _empty_or_unknown(result.stderr)
        return _parse_sessions(result.stdout)

    def attached_sessions(self) -> set[str] | None:
        """The subset of live sessions that have a client attached, or None
        if that could not be determined (#32).

        Display-only: a card reads "attached" (the user already reopened it)
        instead of "restored" when its tmux session is in this set. Mirrors
        ``list_sessions``' tri-state exactly — a timeout, an OSError, or an
        unrecognized nonzero exit is None, never a fabricated "nothing is
        attached", so the badge falls back to "restored" rather than making
        a false claim about a session the user may in fact be sitting in.
        """
        try:
            result = subprocess.run(
                _attached_sessions_cmd(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return _empty_or_unknown(result.stderr)
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

    def current_session_name(self) -> str | None:
        """The name of the tmux session this process is in, or None.

        Used only to target ``link-window`` at the caller's current session
        when they run crr from inside tmux. None (not in tmux, or unreadable)
        is not guessed at — the caller falls back to the aggregate path.
        """
        try:
            result = subprocess.run(
                _current_session_cmd(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        name = result.stdout.strip()
        return name or None

    def capture_pane(self, name: str) -> str | None:
        """Best-effort snapshot of ``name``'s pane text (``-J``: wrapped
        lines joined — see ``_capture_pane_cmd``), or None on any failure.

        Used by the dashboard reauth flow to scrape the OAuth URL out of
        `claude auth login`'s output. A missing session (already exited,
        never started) is not distinguished from a transient error — both
        are "nothing to read" to the caller, which just tries again on the
        next poll.
        """
        try:
            result = subprocess.run(
                _capture_pane_cmd(name),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def send_keys(self, name: str, text: str) -> None:
        """Best-effort key send + Enter into ``name``'s pane.

        Errors are swallowed rather than raised: the dashboard reauth POST
        that calls this is deliberately non-blocking (it returns before
        knowing whether the code was accepted), so a failed send surfaces
        as "no credential refresh on the next poll", not as a 5xx here.
        """
        try:
            subprocess.run(
                _send_keys_cmd(name, text),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
