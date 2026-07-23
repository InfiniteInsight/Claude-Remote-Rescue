"""Configuration: one TOML file, ``config.toml`` in the state-dir parent.

Keys (all optional; unknown keys are ignored):

    web_port = 8377                 # dashboard listen port (loopback only)
    host_allowlist = ["mybox.lan"]  # extra Host-header values, exact match
    archive_retention_days = 30     # gc: prune archive files older than this

Parsing uses stdlib ``tomllib`` (Python >= 3.11). On older interpreters
(pyproject allows >= 3.9) the file is ignored with a one-line stderr
warning and defaults apply -- configuration is a convenience, never a
hard dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

from . import journal

try:  # Python >= 3.11
    import tomllib
except ImportError:  # pragma: no cover - exercised via monkeypatch on 3.11
    tomllib = None  # type: ignore[assignment]

DEFAULT_WEB_PORT = 8377
DEFAULT_RETENTION_DAYS = 30


def defaults() -> Dict:
    return {
        "web_port": DEFAULT_WEB_PORT,
        "host_allowlist": [],
        "archive_retention_days": DEFAULT_RETENTION_DAYS,
    }


def config_path() -> Path:
    """``config.toml`` next to (in the parent of) the state dir."""
    return journal.state_dir().parent / "config.toml"


def load_config() -> Dict:
    """Load and validate the config file, falling back to defaults.

    Every failure mode (missing file, unreadable file, malformed TOML,
    wrong value types, missing tomllib) degrades to defaults -- a broken
    config must never take the dashboard down.
    """
    cfg = defaults()
    path = config_path()
    if not path.is_file():
        return cfg
    if tomllib is None:
        print(
            "crr: warning: %s ignored (tomllib requires Python >= 3.11);"
            " using defaults" % path,
            file=sys.stderr,
        )
        return cfg
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as exc:  # TOMLDecodeError subclasses ValueError
        print("crr: warning: could not parse %s (%s); using defaults" % (path, exc), file=sys.stderr)
        return cfg
    if not isinstance(data, dict):
        return cfg

    port = data.get("web_port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        cfg["web_port"] = port

    hosts = data.get("host_allowlist")
    if isinstance(hosts, list):
        cfg["host_allowlist"] = [h for h in hosts if isinstance(h, str) and h.strip()]

    days = data.get("archive_retention_days")
    if isinstance(days, int) and not isinstance(days, bool) and days >= 0:
        cfg["archive_retention_days"] = days

    return cfg
