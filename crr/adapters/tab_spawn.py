"""macOS tab-spawn adapters (implement crr.core.ports.TabSpawner).

Opens a *visible* terminal tab via ``osascript``: Terminal.app's ``do
script`` or iTerm2's ``write text``. The revived session lives in a
detached tmux session; the tab just runs ``tmux attach`` so closing the
tab never kills the conversation.

The AppleScript *builders* are pure so they are fully unit-testable
without a Mac (running a real tab needs an Aqua session and would trip TCC
automation prompts). Two escaping layers, applied in order:

1. argv → shell string, each element via ``shlex.quote`` (shell-safe).
2. shell string → AppleScript double-quoted literal, escaping ``\\`` then
   ``"`` (AppleScript-safe).

``shlex.quote`` wraps in single quotes, which need no AppleScript escaping,
so the layers compose without interfering — a hostile argv cannot break out
of either. ``open_tab``/``available`` are one-line ``subprocess`` wrappers
around the pure builders so the untested (Mac-GUI) surface is minimal.

Selected in crr.cli by platform + config; no core port selection here (an
adapter never decides which adapter runs).
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Mapping, Sequence

from crr.core.ports import TabSpawnTimeout


def _shell_command(argv: Sequence[str], cwd: str | None) -> str:
    """Render argv (word-form) into a shell command string, cd'ing if asked."""
    cmd = " ".join(shlex.quote(a) for a in argv)
    if cwd:
        return f"cd {shlex.quote(cwd)} && {cmd}"
    return cmd


def _as_applescript_string(s: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal.

    Backslash first (so the quote-escape's backslashes aren't re-escaped),
    then the double-quote.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def terminal_applescript(argv: Sequence[str], cwd: str | None = None) -> str:
    """AppleScript that runs the command in a new Terminal.app tab/window."""
    literal = _as_applescript_string(_shell_command(argv, cwd))
    return f'tell application "Terminal" to do script "{literal}"'


def iterm_applescript(argv: Sequence[str], cwd: str | None = None) -> str:
    """AppleScript that runs the command in a new iTerm2 window's session."""
    literal = _as_applescript_string(_shell_command(argv, cwd))
    return (
        'tell application "iTerm"\n'
        "  set w to (create window with default profile)\n"
        f'  tell current session of w to write text "{literal}"\n'
        "end tell"
    )


def choose(config_terminal: str, env: Mapping[str, str]) -> str:
    """Pick the terminal kind ('terminal' | 'iterm'): config, then env.

    An explicit config value wins (a named prior). Otherwise ``$TERM_PROGRAM``
    is consulted, falling back to Terminal.app which is always installed.
    """
    if config_terminal in ("terminal", "iterm"):
        return config_terminal
    if env.get("TERM_PROGRAM") == "iTerm.app":
        return "iterm"
    return "terminal"


class _OsascriptSpawner:
    """Shared osascript plumbing; subclasses supply the app + script builder."""

    APP_NAME: str = ""

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def _script(self, argv: Sequence[str], cwd: str | None) -> str:  # pragma: no cover
        raise NotImplementedError

    def available(self) -> bool:
        # `open -Ra <App>` exits 0 iff the app is registered with Launch
        # Services — without launching it.
        try:
            return subprocess.run(
                ["open", "-Ra", self.APP_NAME],
                capture_output=True, timeout=self._timeout,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def open_tab(self, argv: Sequence[str], cwd: str | None = None) -> None:
        try:
            subprocess.run(
                ["osascript", "-e", self._script(argv, cwd)],
                capture_output=True, text=True, timeout=self._timeout, check=True,
            )
        except subprocess.TimeoutExpired as exc:
            # A cold terminal app can outrun the budget and still open the
            # tab. Report "could not confirm", never "failed" (#53).
            raise TabSpawnTimeout(exc.timeout or self._timeout) from exc


class TerminalAppSpawner(_OsascriptSpawner):
    APP_NAME = "Terminal"

    def _script(self, argv: Sequence[str], cwd: str | None) -> str:
        return terminal_applescript(argv, cwd)


class ITerm2Spawner(_OsascriptSpawner):
    APP_NAME = "iTerm"

    def _script(self, argv: Sequence[str], cwd: str | None) -> str:
        return iterm_applescript(argv, cwd)


def spawner_for(kind: str, timeout_seconds: float) -> _OsascriptSpawner:
    """Build the spawner for a ``choose()`` result."""
    if kind == "iterm":
        return ITerm2Spawner(timeout_seconds)
    return TerminalAppSpawner(timeout_seconds)
