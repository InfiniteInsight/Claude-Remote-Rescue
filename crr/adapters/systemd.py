"""systemd watchdog adapter (Linux) — generates the revive timer + service.

A user timer fires ``crr revive`` on a fixed interval so revival is
autonomous. This adapter only *builds and writes* the unit files and
reports the enable/linger commands as data; running them (a real change to
the live user manager) is the composition root's job on explicit request,
never a side effect of generation or tests.

The unit environment is made self-sufficient because a user service does
NOT inherit the interactive shell's exported vars:

- ``XDG_STATE_HOME`` is baked from the resolved value, or the service would
  fall back to the default and watch the wrong (empty) state dir — silently
  reviving nothing ([lesson: interop PATH], generalized to state).
- ``PATH`` is derived from the resolved locations of the binaries
  ``crr revive`` actually calls (tmux, ps, claude) plus system dirs — not a
  blanket copy of the current $PATH, which carries venv/ephemeral entries
  absent at service runtime. A missing ``claude`` on PATH would make every
  revived session die instantly on exec.

Invoked only from crr.cli; no core port (core never spawns services).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

SERVICE_NAME = "crr-revive.service"
TIMER_NAME = "crr-revive.timer"
WEB_SERVICE_NAME = "crr-web.service"

# Binaries `crr revive` shells out to (directly or via the revived command).
SERVICE_BINARIES = ("tmux", "ps", "claude")

_SYSTEM_PATH_DIRS = (
    "/usr/local/sbin", "/usr/local/bin",
    "/usr/sbin", "/usr/bin", "/sbin", "/bin",
)


def resolve_service_path(crr_bin: str) -> tuple[str, list[str]]:
    """Return (PATH string, list of unresolved SERVICE_BINARIES names).

    Dirs are ordered: crr's own dir, then the dirs of each resolved service
    binary, then existing system dirs — deduped, order-preserving.
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
    for d in _SYSTEM_PATH_DIRS:
        if os.path.isdir(d):
            add(d)
    return ":".join(dirs), missing


def revive_service_unit(crr_bin: str, path: str, state_home: str) -> str:
    # KillMode=process is load-bearing, not a nicety: this is a Type=oneshot
    # service whose whole job is to spawn DETACHED tmux sessions and exit.
    # Under the default KillMode=control-group, systemd reaps every process in
    # the service's cgroup when the oneshot finishes — including the tmux
    # server it just started — so the revived sessions die the instant the
    # watchdog completes (a silent, total failure of revival). KillMode=process
    # kills only crr itself on stop and leaves the tmux server alone.
    # (Found on real systemd during the hardware acceptance test.)
    return (
        "[Unit]\n"
        "Description=Claude-Remote-Rescue watchdog (revive crashed claude sessions)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "KillMode=process\n"
        f"Environment=PATH={path}\n"
        f"Environment=XDG_STATE_HOME={state_home}\n"
        f"ExecStart={crr_bin} revive\n"
    )


def revive_timer_unit(interval_seconds: int) -> str:
    return (
        "[Unit]\n"
        "Description=Claude-Remote-Rescue watchdog timer\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={interval_seconds}s\n"
        f"OnUnitActiveSec={interval_seconds}s\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def web_service_unit(crr_bin: str, path: str, state_home: str, port: int) -> str:
    """A long-running dashboard service (loopback; tailnet-served).

    Type=simple + Restart so the dashboard stays up across crashes, and
    WantedBy=default.target + linger so it survives logout and comes back
    at (re)boot — the ROADMAP's "dashboard reachable after reboot".
    """
    return (
        "[Unit]\n"
        "Description=Claude-Remote-Rescue dashboard (loopback; tailnet-served)\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=PATH={path}\n"
        f"Environment=XDG_STATE_HOME={state_home}\n"
        f"ExecStart={crr_bin} web --port {port}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def unit_dir(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def write_units(target_dir: Path, units: dict[str, str]) -> list[Path]:
    """Write ``{filename: content}`` units; return their paths."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in units.items():
        p = target_dir / name
        p.write_text(content, encoding="utf-8")
        paths.append(p)
    return paths


def enable_commands() -> list[list[str]]:
    """The commands that activate the watchdog + dashboard (data, not run).

    linger lets the user manager run the timer and the dashboard without an
    active login — essential on a headless box reached only by SSH.
    """
    return [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", TIMER_NAME],
        ["systemctl", "--user", "enable", "--now", WEB_SERVICE_NAME],
        ["loginctl", "enable-linger"],
    ]


def disable_commands() -> list[list[str]]:
    """The commands that deactivate the watchdog + dashboard (data, not run).

    Mirror of enable_commands; linger is left alone (other services may
    rely on it — enabling it was additive, so removal is the user's call).
    """
    return [
        ["systemctl", "--user", "disable", "--now", TIMER_NAME],
        ["systemctl", "--user", "disable", "--now", WEB_SERVICE_NAME],
        ["systemctl", "--user", "daemon-reload"],
    ]
