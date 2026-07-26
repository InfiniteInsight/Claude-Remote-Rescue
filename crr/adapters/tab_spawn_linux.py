"""Linux desktop tab-spawn adapters (implement crr.core.ports.TabSpawner).

The Linux counterparts to the macOS osascript spawners: open a visible
terminal window/tab running ``argv`` (for reopen, ``tmux attach -t
crr-<sid8>``). Unlike macOS, these terminals accept an argv **directly**, so
there is no shell string and no escaping layer — ``[lesson: word-form
exec]`` is satisfied by construction. Each builder is a pure ~1-liner; the
spawner is a thin ``subprocess`` wrapper.

Selection is config-first (``terminal`` prior), then ``$TERM_PROGRAM``, then
the first installed terminal in a priority order. ``detect`` additionally
refuses to spawn on a headless session (no ``$DISPLAY``/``$WAYLAND_DISPLAY``)
— there are no tabs there, so reopen degrades to detached tmux.

Selected in crr.cli by platform + config; an adapter never decides which
adapter runs.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable, Mapping, Sequence

# Priority order for auto-detection (also the set of valid explicit choices).
_PRIORITY = ("gnome-terminal", "konsole", "kitty", "wezterm")

# $TERM_PROGRAM values → our terminal kind (case-insensitive match below).
_TERM_PROGRAM = {"wezterm": "wezterm", "konsole": "konsole", "kitty": "kitty"}


def gnome_terminal_command(argv: Sequence[str], cwd: str | None) -> list[str]:
    wd = ["--working-directory", cwd] if cwd else []
    return ["gnome-terminal", *wd, "--", *argv]


def konsole_command(argv: Sequence[str], cwd: str | None) -> list[str]:
    wd = ["--workdir", cwd] if cwd else []
    return ["konsole", *wd, "-e", *argv]


def kitty_command(argv: Sequence[str], cwd: str | None) -> list[str]:
    wd = ["--directory", cwd] if cwd else []
    return ["kitty", *wd, *argv]


def wezterm_command(argv: Sequence[str], cwd: str | None) -> list[str]:
    wd = ["--cwd", cwd] if cwd else []
    return ["wezterm", "cli", "spawn", *wd, "--", *argv]


_BUILDERS: dict[str, Callable[[Sequence[str], str | None], list[str]]] = {
    "gnome-terminal": gnome_terminal_command,
    "konsole": konsole_command,
    "kitty": kitty_command,
    "wezterm": wezterm_command,
}


def choose_kind(
    config_terminal: str,
    env: Mapping[str, str],
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Return the terminal kind to use, or None if none is installed.

    An explicit, installed Linux terminal in config wins. Otherwise (``auto``
    or a non-Linux value like a macOS choice) fall back to ``$TERM_PROGRAM``
    if it names a known installed terminal, then the first installed terminal
    in priority order. ``which`` defaults to ``shutil.which`` resolved at call
    time (so it honors monkeypatching), overridable for tests.
    """
    which = which or shutil.which
    if config_terminal in _BUILDERS and which(config_terminal):
        return config_terminal
    prog = _TERM_PROGRAM.get(env.get("TERM_PROGRAM", "").lower())
    if prog and which(prog):
        return prog
    for kind in _PRIORITY:
        if which(kind):
            return kind
    return None


def _has_display(env: Mapping[str, str]) -> bool:
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


class LinuxTerminalSpawner:
    """TabSpawner for a specific Linux desktop terminal."""

    def __init__(self, kind: str, timeout_seconds: float) -> None:
        self.kind = kind
        self._timeout = timeout_seconds

    def available(self) -> bool:
        return shutil.which(self.kind) is not None

    def open_tab(self, argv: Sequence[str], cwd: str | None = None) -> None:
        subprocess.run(
            _BUILDERS[self.kind](argv, cwd),
            capture_output=True, text=True, timeout=self._timeout, check=True,
        )


def detect(
    config_terminal: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    which: Callable[[str], str | None] | None = None,
) -> LinuxTerminalSpawner | None:
    """The Linux desktop spawner for this session, or None.

    None when headless (no display → no tabs) or when no known terminal is
    installed, so reopen degrades to detached tmux instead of erroring.
    """
    if not _has_display(env):
        return None
    kind = choose_kind(config_terminal, env, which=which)
    return LinuxTerminalSpawner(kind, timeout_seconds) if kind else None
