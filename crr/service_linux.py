"""Linux service manager adapter: systemd user units.

- ``crr-web.service``: runs ``crr web`` (the dashboard server).
- ``crr-watchdog.service`` + ``crr-watchdog.timer``: runs
  ``crr revive --all`` every couple of minutes so crashed sessions with a
  claude sid come back without anyone touching the dashboard.

Install writes the unit files, daemon-reloads, enables (and starts) both
units, and runs ``loginctl enable-linger`` so the units keep running
without an active login session. Every step's success/failure is
reported individually and never swallowed ([lesson] a swallowed exit
code once turned hard failures into green checkmarks, same principle
applied here to service install).

[lesson: interop PATH] Units get an explicit, self-sufficient
``Environment=PATH=...`` line covering every external binary the unit's
command (transitively) needs -- ``tmux``, ``ps``, ``claude``, and the
``crr`` script itself -- resolved once at install time. A missing dir
here silently broke diagnostics before this was made explicit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

UNIT_DIR_ENV = "CRR_SYSTEMD_USER_DIR"

WEB_SERVICE_NAME = "crr-web.service"
WATCHDOG_SERVICE_NAME = "crr-watchdog.service"
WATCHDOG_TIMER_NAME = "crr-watchdog.timer"

WATCHDOG_INTERVAL = "2min"

# External binaries any unit's ExecStart transitively relies on (directly,
# or via crr's own subprocess calls -- ops.py/classify.py shell out to
# `ps`, revive.py to `tmux`, and revival/watchdog both eventually exec
# `claude`).
_EXTERNAL_BINARIES = ("tmux", "ps", "claude")

_FALLBACK_PATH_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")


def systemd_user_dir() -> Path:
    override = os.environ.get(UNIT_DIR_ENV)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def resolve_unit_path(crr_bin: str) -> str:
    """Build the PATH covering *crr_bin* plus every external binary a unit
    invokes, resolved once here (install time) so the unit stays
    self-sufficient inside systemd's minimal default environment."""
    dirs: List[str] = []

    def add(p: Optional[str]) -> None:
        if not p:
            return
        d = str(Path(p).resolve().parent)
        if d not in dirs:
            dirs.append(d)

    add(crr_bin)
    for binname in _EXTERNAL_BINARIES:
        add(shutil.which(binname))
    for fallback in _FALLBACK_PATH_DIRS:
        if fallback not in dirs:
            dirs.append(fallback)
    return ":".join(dirs)


# ---------------------------------------------------------------------------
# Unit file contents


def web_service_unit(crr_bin: str) -> str:
    path = resolve_unit_path(crr_bin)
    return (
        "[Unit]\n"
        "Description=Claude-Remote-Rescue web dashboard\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "Environment=PATH=%s\n"
        "ExecStart=%s web\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n" % (path, crr_bin)
    )


def watchdog_service_unit(crr_bin: str) -> str:
    path = resolve_unit_path(crr_bin)
    return (
        "[Unit]\n"
        "Description=Claude-Remote-Rescue watchdog (revive crashed sessions)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "Environment=PATH=%s\n"
        "ExecStart=%s revive --all\n" % (path, crr_bin)
    )


def watchdog_timer_unit() -> str:
    return (
        "[Unit]\n"
        "Description=Run %s every %s\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=%s\n"
        "OnUnitActiveSec=%s\n"
        "Unit=%s\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
        % (
            WATCHDOG_SERVICE_NAME,
            WATCHDOG_INTERVAL,
            WATCHDOG_INTERVAL,
            WATCHDOG_INTERVAL,
            WATCHDOG_SERVICE_NAME,
        )
    )


# ---------------------------------------------------------------------------
# systemctl / loginctl driving


def _run(argv: List[str]) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or (
            "exit %d" % proc.returncode
        )
        return False, detail
    return True, (proc.stdout or "").strip()


def install(crr_bin: str) -> List[Dict]:
    """Write units, daemon-reload, enable+start both units, enable-linger.

    Every step runs regardless of earlier failures (so a full report
    comes back in one call); each step's ok/detail is reported, never
    swallowed.
    """
    steps: List[Dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    unit_dir = systemd_user_dir()
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / WEB_SERVICE_NAME).write_text(web_service_unit(crr_bin), encoding="utf-8")
        (unit_dir / WATCHDOG_SERVICE_NAME).write_text(
            watchdog_service_unit(crr_bin), encoding="utf-8"
        )
        (unit_dir / WATCHDOG_TIMER_NAME).write_text(watchdog_timer_unit(), encoding="utf-8")
        record("write-units", True, str(unit_dir))
    except OSError as exc:
        record("write-units", False, str(exc))
        return steps  # nothing downstream can succeed without the unit files

    ok, detail = _run(["systemctl", "--user", "daemon-reload"])
    record("daemon-reload", ok, detail)

    ok, detail = _run(["systemctl", "--user", "enable", "--now", WEB_SERVICE_NAME])
    record("enable-web", ok, detail)

    ok, detail = _run(["systemctl", "--user", "enable", "--now", WATCHDOG_TIMER_NAME])
    record("enable-watchdog-timer", ok, detail)

    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    linger_argv = ["loginctl", "enable-linger"] + ([user] if user else [])
    ok, detail = _run(linger_argv)
    record("enable-linger", ok, detail)

    return steps


def uninstall() -> List[Dict]:
    steps: List[Dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    ok, detail = _run(["systemctl", "--user", "disable", "--now", WEB_SERVICE_NAME])
    record("disable-web", ok, detail)

    ok, detail = _run(["systemctl", "--user", "disable", "--now", WATCHDOG_TIMER_NAME])
    record("disable-watchdog-timer", ok, detail)

    unit_dir = systemd_user_dir()
    removed = []
    for name in (WEB_SERVICE_NAME, WATCHDOG_SERVICE_NAME, WATCHDOG_TIMER_NAME):
        path = unit_dir / name
        try:
            path.unlink()
            removed.append(name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            record("remove-%s" % name, False, str(exc))
    record("remove-units", True, ",".join(removed))

    ok, detail = _run(["systemctl", "--user", "daemon-reload"])
    record("daemon-reload", ok, detail)
    return steps


def status() -> List[Dict]:
    out = []
    for name in (WEB_SERVICE_NAME, WATCHDOG_SERVICE_NAME, WATCHDOG_TIMER_NAME):
        ok, detail = _run(["systemctl", "--user", "is-active", name])
        out.append({"unit": name, "active": detail or ("active" if ok else "inactive"), "ok": ok})
    return out
