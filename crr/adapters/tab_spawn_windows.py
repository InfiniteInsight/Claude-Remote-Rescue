"""Windows Terminal tab-spawn adapter (implements crr.core.ports.TabSpawner).

crr runs inside WSL; a "visible tab" on this host is a Windows Terminal tab
that re-enters the WSL distro (``wsl.exe -e``) and runs the word-form argv
(for reopen, ``tmux attach -t crr-<sid8>``). Like every spawner it takes
argv directly — no shell string, so ``[lesson: word-form exec]`` holds.

Ported from ccresume's ``wt.exe new-tab -p <profile>`` launcher. Selected in
crr.cli only when running under WSL with ``wt.exe`` reachable.

UNVERIFIED from Linux CI: the builder logic is unit-tested, but the real
wt.exe/wsl.exe round-trip is author-verified on Windows (task #8).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Sequence


def wt_command(
    argv: Sequence[str],
    cwd: str | None = None,
    profile: str = "",
    distro: str | None = None,
) -> list[str]:
    """Build the ``wt.exe new-tab`` command that runs ``argv`` inside WSL."""
    cmd = ["wt.exe", "new-tab"]
    if profile:
        cmd += ["-p", profile]
    if cwd:
        cmd += ["-d", cwd]  # wt's starting directory
    cmd += ["wsl.exe"]
    if distro:
        cmd += ["--distribution", distro]
    cmd += ["-e", *argv]
    return cmd


class WindowsTerminalSpawner:
    """TabSpawner backed by wt.exe (WSL host)."""

    def __init__(self, timeout_seconds: float, profile: str = "", distro: str | None = None) -> None:
        self._timeout = timeout_seconds
        self._profile = profile
        self._distro = distro

    def available(self) -> bool:
        return shutil.which("wt.exe") is not None

    def open_tab(self, argv: Sequence[str], cwd: str | None = None) -> None:
        subprocess.run(
            wt_command(argv, cwd, self._profile, self._distro),
            capture_output=True, text=True, timeout=self._timeout, check=True,
        )
