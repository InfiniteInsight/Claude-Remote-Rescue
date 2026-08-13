"""launchd user-agent adapter (macOS) — generates the revive + web + awake
agents.

The macOS analogue of ``systemd.py``. A user agent in
``~/Library/LaunchAgents`` fires ``crr revive`` on an interval, keeps
``crr web`` alive, and keeps ``crr awake`` alive, so revival, the
dashboard, and the keep-awake hold are all autonomous. This adapter only
*builds and writes* the plist files and reports the load commands as data;
running ``launchctl`` (a real change to the live user domain) is the
composition root's job on explicit request, never a side effect of
generation or tests.

Plists are serialized with ``plistlib`` (stdlib — no runtime dep, correct
Apple XML + DOCTYPE + escaping) rather than hand-written strings.

PATH is baked because launchd hands agents a minimal default PATH
(``/usr/bin:/bin:/usr/sbin:/sbin``) that excludes both Homebrew prefixes —
exactly where ``claude`` and ``tmux`` live on a Mac. A missing binary on
that PATH makes every revived session die instantly on exec
([lesson: interop PATH]).

State dir is deliberately NOT baked: ``state_dir.resolve("Darwin", …)`` is
env-independent (always ``~/Library/Application Support/crr``), so the agent
resolves the same dir the shims write to using only HOME, which the agent's
context already carries. Baking anything would be dead code.

Invoked only from crr.cli; no core port (core never spawns services).
"""

from __future__ import annotations

import os
import plistlib
import shutil
from pathlib import Path

REVIVE_LABEL = "com.claude-remote-rescue.revive"
WEB_LABEL = "com.claude-remote-rescue.web"
AWAKE_LABEL = "com.claude-remote-rescue.awake"
REVIVE_PLIST = REVIVE_LABEL + ".plist"
WEB_PLIST = WEB_LABEL + ".plist"
AWAKE_PLIST = AWAKE_LABEL + ".plist"

# Binaries `crr revive` shells out to (directly or via the revived command).
SERVICE_BINARIES = ("tmux", "ps", "claude")

# launchd's minimal default PATH plus BOTH Homebrew prefixes, so a revival
# resolves claude/tmux on Apple Silicon (/opt/homebrew) or Intel
# (/usr/local) regardless of the interactive shell's PATH at generate time.
_MAC_PATH_DIRS = (
    "/opt/homebrew/bin", "/opt/homebrew/sbin",   # Apple Silicon Homebrew
    "/usr/local/bin", "/usr/local/sbin",          # Intel Homebrew
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",     # launchd default
)


def resolve_service_path(crr_bin: str) -> tuple[str, list[str]]:
    """Return (PATH string, list of unresolved SERVICE_BINARIES names).

    Dirs are ordered: crr's own dir, then the dirs of each resolved service
    binary, then existing macOS dirs — deduped, order-preserving. Parallel
    to (not shared with) systemd's resolver: the baseline dirs differ.
    """
    dirs: list[str] = []

    def add(d: str) -> None:
        if d and d not in dirs:
            dirs.append(d)

    add(os.path.dirname(os.path.abspath(crr_bin)))
    missing: list[str] = []
    for name in SERVICE_BINARIES:
        found = shutil.which(name)
        if found:
            add(os.path.dirname(os.path.abspath(found)))
        else:
            missing.append(name)
    for d in _MAC_PATH_DIRS:
        if os.path.isdir(d):
            add(d)
    return ":".join(dirs), missing


def revive_agent_plist(crr_bin: str, path: str, interval_seconds: int) -> str:
    # NOTE: systemd needed KillMode=process here (its cgroup cleanup reaps the
    # detached tmux the revive spawns). launchd is process-group based, not
    # cgroup based, and `tmux new-session -d` setsid-daemonizes into its own
    # session — so it should survive the agent exiting without an equivalent
    # directive. This must be confirmed on the macOS hardware acceptance test;
    # if a revived session dies when the agent finishes, add
    # AbandonProcessGroup here (the launchd analogue of KillMode=process).
    return plistlib.dumps({
        "Label": REVIVE_LABEL,
        "ProgramArguments": [crr_bin, "revive"],
        "StartInterval": interval_seconds,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": path},
    }).decode("utf-8")


def web_agent_plist(crr_bin: str, path: str, port: int) -> str:
    """The dashboard agent (loopback; tailnet-served).

    KeepAlive + RunAtLoad keep the dashboard up while logged in and bring it
    back at login. This is NOT headless-across-reboot like Linux linger:
    user agents run only in the Aqua login session, and DESIGN.md
    deliberately chose user agents over a LaunchDaemon.
    """
    return plistlib.dumps({
        "Label": WEB_LABEL,
        "ProgramArguments": [crr_bin, "web", "--port", str(port)],
        "KeepAlive": True,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": path},
    }).decode("utf-8")


def awake_agent_plist(crr_bin: str, path: str) -> str:
    """The loop that holds the machine awake while a session is live.

    Its own agent rather than a job inside the web agent: the hold is a
    child of whatever process owns it, and tying that to the dashboard
    would couple "am I serving a page" to "may this machine sleep".

    KeepAlive + RunAtLoad keep the loop running while logged in and bring
    it back at login — same rationale as ``web_agent_plist``. No
    StartInterval: `crr awake` is a long-running loop, not a periodic job,
    and a StartInterval would spawn a second loop alongside the first, with
    two holders fighting over the same machine.
    """
    return plistlib.dumps({
        "Label": AWAKE_LABEL,
        "ProgramArguments": [crr_bin, "awake"],
        "KeepAlive": True,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": path},
    }).decode("utf-8")


def agent_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def write_agents(target_dir: Path, agents: dict[str, str]) -> list[Path]:
    """Write ``{filename: content}`` plists; return their paths."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in agents.items():
        p = target_dir / name
        p.write_text(content, encoding="utf-8")
        paths.append(p)
    return paths


def enable_commands(target_dir: Path) -> list[list[str]]:
    """The commands that load all agents (data, not run).

    ``launchctl load -w`` marks the agents enabled and starts them; they
    then reload automatically at each login.
    """
    target_dir = Path(target_dir)
    return [
        ["launchctl", "load", "-w", str(target_dir / REVIVE_PLIST)],
        ["launchctl", "load", "-w", str(target_dir / WEB_PLIST)],
        ["launchctl", "load", "-w", str(target_dir / AWAKE_PLIST)],
    ]


def disable_commands(target_dir: Path) -> list[list[str]]:
    """The commands that unload all agents (data, not run).

    Callers must unload BEFORE removing the plist files — launchctl needs
    the plist present on disk to unload it.
    """
    target_dir = Path(target_dir)
    return [
        ["launchctl", "unload", "-w", str(target_dir / REVIVE_PLIST)],
        ["launchctl", "unload", "-w", str(target_dir / WEB_PLIST)],
        ["launchctl", "unload", "-w", str(target_dir / AWAKE_PLIST)],
    ]
