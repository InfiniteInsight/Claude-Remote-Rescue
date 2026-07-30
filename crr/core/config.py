"""Config priors (audit P5 — Injectable priors, P3 — visible origin).

Every constant that encodes a judgment call lives here as a named key
with a versioned default — never a magic number in logic. New timing or
threshold decisions join ``DEFAULTS`` at introduction time, not at audit
time (that rule is why this module exists before most of its consumers
do).

``effective()`` reports each key's value AND its origin (``configured``
vs ``default``) so a consumer can always distinguish an explicit choice
from an assumed one — defaults drive kill decisions, and an invisible
default is an invisible prior. It backs ``crr config --effective``.

TOML loading lives here too (``load_toml_overrides``); ``Config`` still
takes a plain overrides mapping so tests need no files.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Mapping

# Bump when a default value changes meaning, so a consumer pinning
# behavior can detect the shift.
# v2: dropped watcher_backoff_count / watcher_cooldown_seconds / reopen_grace_seconds
# (no crr mechanism consumes them; see DESIGN)
CONFIG_DEFAULTS_VERSION = 2

# The audit "config floor": each of these was a hardcoded prior the audit
# caught (or a peer of one). Value is the versioned default.
DEFAULTS: dict[str, Any] = {
    # watchdog / revival
    "zombie_strikes": 3,             # strikes before a re-dying session is archived
    "watchdog_interval_seconds": 30, # how often the systemd timer sweeps for revivals
    # session operations
    "close_grace_seconds": 5,     # wait after a polite close before force
    # diagnostics
    "diagnose_lookback_boots": 1,  # how many prior boots to inspect
    "diagnose_event_cap": 50,      # max events returned per source
    "diagnose_line_cap": 200,      # max log lines scanned per source
    "diagnose_macos_lookback": "1d",  # macOS `log show --last` window (no boot index on macOS)
    "diagnose_macos_timeout_seconds": 30,  # macOS `log show` is slow to start; own timeout
    "interop_timeout_seconds": 5,  # per external-command guard in diagnostics/probes
    # dashboard
    "dashboard_poll_seconds": 5,   # session-list poll cadence
    "version_check_seconds": 30,   # page self-heal version poll cadence
    "last_prompt_display_cap": 280,  # chars of last prompt shown on a card
    "host_allowlist_extras": [],   # extra Host values the dashboard accepts
    # retention
    "archive_retention_days": 14,  # gc drops archive records older than this
    # tab spawn (macOS/desktop): which terminal a visible reopen uses.
    # "auto" = $TERM_PROGRAM then a sensible default; or force one — macOS:
    # "terminal"/"iterm"; Linux: "gnome-terminal"/"konsole"/"kitty"/"wezterm"
    # (a named prior, not a magic default).
    "terminal": "auto",
    "wt_profile": "",  # Windows Terminal profile for a WSL reopen ("" = default)
}


class ConfigError(ValueError):
    """A config override is invalid (unknown key or wrong value type)."""


def load_toml_overrides(path: Path | str) -> dict[str, Any]:
    """Read a config.toml into an overrides dict ({} if the file is absent)."""
    path = Path(path)
    if not path.is_file():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


class Config:
    def __init__(self, overrides: Mapping[str, Any] | None = None) -> None:
        overrides = dict(overrides or {})
        unknown = set(overrides) - set(DEFAULTS)
        if unknown:
            raise ConfigError(f"unknown config key(s): {sorted(unknown)}")
        # Type-check each override against its default so a mistyped config.toml
        # fails loudly rather than injecting e.g. a str where logic wants an int.
        for key, value in overrides.items():
            default = DEFAULTS[key]
            # bool is an int subclass; keep them from cross-matching.
            if isinstance(value, bool) != isinstance(default, bool) or not isinstance(value, type(default)):
                raise ConfigError(
                    f"config '{key}' must be {type(default).__name__}, got {type(value).__name__}"
                )
        self._overrides = overrides

    def get(self, key: str) -> Any:
        if key not in DEFAULTS:
            raise KeyError(key)
        return self._overrides.get(key, DEFAULTS[key])

    def effective(self) -> dict[str, tuple[Any, str]]:
        """Return ``{key: (value, origin)}`` for every known key."""
        return {
            key: (
                (self._overrides[key], "configured")
                if key in self._overrides
                else (default, "default")
            )
            for key, default in DEFAULTS.items()
        }
