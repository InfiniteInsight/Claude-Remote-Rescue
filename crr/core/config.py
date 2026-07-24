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

TOML file loading is intentionally not here yet; ``Config`` takes an
``overrides`` mapping so the priors and their origins are usable now,
with the file loader layered on in a later increment without changing
this contract.
"""

from __future__ import annotations

from typing import Any, Mapping

# Bump when a default value changes meaning, so a consumer pinning
# behavior can detect the shift.
CONFIG_DEFAULTS_VERSION = 1

# The audit "config floor": each of these was a hardcoded prior the audit
# caught (or a peer of one). Value is the versioned default.
DEFAULTS: dict[str, Any] = {
    # watchdog / revival
    "zombie_strikes": 3,             # strikes before a re-dying session is archived
    "watchdog_interval_seconds": 30, # how often the systemd timer sweeps for revivals
    "watcher_backoff_count": 5,      # consecutive revival attempts before backing off
    "watcher_cooldown_seconds": 60,
    # session operations
    "close_grace_seconds": 5,     # wait after a polite close before force
    "reopen_grace_seconds": 5,    # wait for a revived tab to register
    # diagnostics
    "diagnose_lookback_boots": 1,  # how many prior boots to inspect
    "diagnose_event_cap": 50,      # max events returned per source
    "diagnose_line_cap": 200,      # max log lines scanned per source
    "interop_timeout_seconds": 5,  # per external-command guard in diagnostics/probes
    # dashboard
    "dashboard_poll_seconds": 5,   # session-list poll cadence
    "version_check_seconds": 30,   # page self-heal version poll cadence
    "last_prompt_display_cap": 280,  # chars of last prompt shown on a card
}


class ConfigError(ValueError):
    """A config override is invalid (e.g. an unknown key)."""


class Config:
    def __init__(self, overrides: Mapping[str, Any] | None = None) -> None:
        overrides = dict(overrides or {})
        unknown = set(overrides) - set(DEFAULTS)
        if unknown:
            raise ConfigError(f"unknown config key(s): {sorted(unknown)}")
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
