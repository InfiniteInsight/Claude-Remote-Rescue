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

from crr.core import tab_health
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
        r = subprocess.run([path, "--version"], capture_output=True, timeout=timeout)
        return r.returncode == 0
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


# The App User Model ID keyed off the package FAMILY name. Verified
# 2026-08-29: launching through the shell bypasses the wt.exe App Execution
# Alias entirely, and arguments pass through. The family name is stable
# across Windows Terminal versions, so unlike a package path it never goes
# stale on upgrade. Directly executing the real wt.exe under
# C:\Program Files\WindowsApps is NOT an option — measured exit 126,
# Permission denied, because of the WindowsApps ACLs.
AUMID = r"shell:appsFolder\Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"


def _ps_quote(value: str) -> str:
    """Single-quote a value for PowerShell, doubling embedded single quotes."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _arg_list(items: Sequence[str]) -> str:
    """Render a PowerShell -ArgumentList array from word-form argv."""
    return ",".join(_ps_quote(item) for item in items)


def aumid_command(
    argv: Sequence[str],
    cwd: str | None = None,
    profile: str = "",
    distro: str | None = None,
) -> list[str]:
    """Tier 2: open a real Windows Terminal tab without the wt.exe alias."""
    inner: list[str] = ["new-tab"]
    if profile:
        inner += ["-p", profile]
    if cwd:
        inner += ["-d", cwd]
    inner += ["wsl.exe"]
    if distro:
        inner += ["--distribution", distro]
    inner += ["-e", *argv]
    return [
        "powershell.exe", "-NoProfile", "-Command",
        f"Start-Process '{AUMID}' -ArgumentList {_arg_list(inner)}",
    ]


def console_command(argv: Sequence[str], distro: str | None = None) -> list[str]:
    """Tier 3: a plain console window running wsl.exe — no Windows Terminal."""
    inner: list[str] = []
    if distro:
        inner += ["--distribution", distro]
    inner += ["-e", *argv]
    return [
        "powershell.exe", "-NoProfile", "-Command",
        f"Start-Process wsl.exe -ArgumentList {_arg_list(inner)}",
    ]


class WindowsTerminalSpawner:
    """TabSpawner backed by wt.exe (WSL host)."""

    def __init__(self, timeout_seconds: float, profile: str = "", distro: str | None = None) -> None:
        self._timeout = timeout_seconds
        self._profile = profile
        self._distro = distro
        # Which tier opened the most recent tab, and whether that tier can
        # actually prove it. Read by crr.cli after a spawn; see the class
        # docstring for why this is an attribute rather than a return value.
        self.last_tier: str | None = None
        self.last_confirmed: bool = False

    def available(self, probe: bool = True) -> bool:
        # wt.exe reachable, interop handler registered, AND (when probe) the
        # binary actually runs. The third catches broken App Execution
        # Aliases AND contexts where wt.exe cannot exec (tmux, systemd).
        #
        # wt_probe (`wt.exe --version`) is the ONLY step that opens a GUI
        # window — wt is a GUI app with no console, so --version pops a
        # dialog. It earns that cost only before a DESTRUCTIVE spawn-before-
        # kill (untmux/detmux), where a wt that returns success without
        # actually opening a tab would cost the user their session. The
        # best-effort callers — reopen and the rescue re-home — pass
        # probe=False: their tab is a convenience on an already-durable tmux
        # session, so a failed open_tab is reported (never lost), and paying
        # a help-window flash on every recovery is not worth it
        # [/exit revival 2026-08-25]. The windowless checks (path + interop)
        # still run, so a truly headless context is still caught.
        path = wt_path()
        if path is None or not interop_registered():
            return False
        return wt_probe(path, self._timeout) if probe else True

    def open_tab(self, argv: Sequence[str], cwd: str | None = None) -> None:
        """Open a visible tab, falling through launcher tiers on failure.

        Tier 1 is wt.exe from PATH (the App Execution Alias). When the alias
        is disabled the stub fails to exec immediately — no hang, no window —
        so falling through costs milliseconds and no UI. Tier 2 reaches the
        same Windows Terminal through the shell AUMID, bypassing the alias.
        Tier 3 drops Windows Terminal entirely for a plain console window.

        A TimeoutExpired never falls through: a cold Windows Terminal can
        outrun the budget and still open the tab (#53), and a second window
        is worse than waiting.
        """
        attempts = (
            (tab_health.TIER_WT,
             wt_command(argv, cwd, self._profile, self._distro), True),
            (tab_health.TIER_AUMID,
             aumid_command(argv, cwd, self._profile, self._distro), False),
            (tab_health.TIER_CONSOLE,
             console_command(argv, self._distro), False),
        )
        last_error: Exception | None = None
        for tier, command, confirmable in attempts:
            try:
                subprocess.run(
                    command, capture_output=True, text=True,
                    timeout=self._timeout, check=True,
                )
            except subprocess.TimeoutExpired as exc:
                self.last_tier = tier
                self.last_confirmed = False
                # A cold Windows Terminal can outrun the budget and still open
                # the tab. Say we could not confirm; do not claim it failed
                # (#53). Never fall through: a second window would be worse.
                raise TabSpawnTimeout(exc.timeout or self._timeout) from exc
            except (subprocess.CalledProcessError, OSError) as exc:
                last_error = exc
                continue
            self.last_tier = tier
            # Tiers 2 and 3 use Start-Process, which returns as soon as the
            # process is launched: a zero exit proves the launch, not the tab.
            self.last_confirmed = confirmable
            return
        self.last_tier = tab_health.TIER_NONE
        self.last_confirmed = False
        assert last_error is not None
        raise last_error
