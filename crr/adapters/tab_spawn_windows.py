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
from pathlib import Path
from typing import Sequence

from crr.core.ports import TabSpawnTimeout

BINFMT_MISC = Path("/proc/sys/fs/binfmt_misc")

# Where WSL mounts the Windows drives. Overridable so the fallback search is
# testable without a real /mnt/c.
MNT_ROOT = Path("/mnt")
# Windows Terminal's per-user install location (the App Execution Alias).
_WINDOWSAPPS_GLOB = "*/Users/*/AppData/Local/Microsoft/WindowsApps/wt.exe"


def wt_path() -> str | None:
    """Absolute path to wt.exe, resolved at CALL time — or None.

    PATH first, which is the normal answer. The fallback exists because
    ``crr systemd`` bakes the WindowsApps directory into the service's PATH
    at install time (a service inherits no Windows dirs), and that snapshot
    goes stale silently when the Windows user profile is renamed or moved —
    leaving tab spawning broken with no signal beyond a degraded reopen
    (#54). Looking under /mnt/*/Users finds it again without a reinstall.
    """
    found = shutil.which("wt.exe")
    if found:
        return found
    try:
        for candidate in sorted(MNT_ROOT.glob(_WINDOWSAPPS_GLOB)):
            return str(candidate)
    except OSError:
        pass
    return None

# WSL registers one of these; newer images use the "-late" variant.
_INTEROP_HANDLERS = ("WSLInterop", "WSLInterop-late")


def interop_registered(binfmt_misc: Path | None = None) -> bool:
    """True when the kernel can exec a Windows PE binary in this namespace.

    ``shutil.which`` cannot answer this: DrvFs marks every file under /mnt/c
    executable, so wt.exe resolves whether or not it can actually run. The
    handler that makes the exec succeed is binfmt_misc's WSLInterop entry —
    and it can go missing while wt.exe stays on PATH, because a systemd
    remount of /proc/sys/fs/binfmt_misc replaces the (empty) filesystem
    instance WSL registered into at boot. Without it, execve returns ENOEXEC
    ([live bug, 2026-08-09]).
    """
    root = BINFMT_MISC if binfmt_misc is None else binfmt_misc
    for name in _INTEROP_HANDLERS:
        try:
            first = (root / name).read_text().split("\n", 1)[0].strip()
        except OSError:
            continue  # absent, or an unreadable /proc — treat as not usable
        if first == "enabled":
            return True
    return False


def wt_probe(path: str, timeout: float) -> bool:
    """True when ``wt.exe --version`` succeeds — the alias actually works."""
    try:
        subprocess.run([path, "--version"], capture_output=True, timeout=timeout)
        return True
    except Exception:
        return False


def wt_command(
    argv: Sequence[str],
    cwd: str | None = None,
    profile: str = "",
    distro: str | None = None,
) -> list[str]:
    """Build the ``wt.exe new-tab`` command that runs ``argv`` inside WSL."""
    cmd = [wt_path() or "wt.exe", "new-tab"]
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
        # Three checks: wt.exe exists, interop handler registered, and the
        # binary actually runs. The third catches broken App Execution Aliases
        # (a 2-byte reparse point stub left after a Windows Terminal update
        # that passes the first two checks but fails with EINVAL on exec).
        path = wt_path()
        if path is None or not interop_registered():
            return False
        return wt_probe(path, self._timeout)

    def open_tab(self, argv: Sequence[str], cwd: str | None = None) -> None:
        try:
            subprocess.run(
                wt_command(argv, cwd, self._profile, self._distro),
                capture_output=True, text=True, timeout=self._timeout, check=True,
            )
        except subprocess.TimeoutExpired as exc:
            # A cold Windows Terminal can outrun the budget and still open the
            # tab. Say we could not confirm; do not claim it failed (#53).
            raise TabSpawnTimeout(exc.timeout or self._timeout) from exc
