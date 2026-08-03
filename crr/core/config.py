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
# v3: added context_tight_fraction / context_compact_fraction (Slice A, F2 —
# compaction badge thresholds; see crr.core.context_pressure)
CONFIG_DEFAULTS_VERSION = 3

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
    # restore prompt
    "rescue_prompt_timeout_seconds": 15,  # [Y/n] wait before defaulting to "not now"
    # dashboard port + service restart cadence (audit 2026-07-31, P5)
    "dashboard_port": 8377,        # `crr web`'s bind port; baked into systemd/launchd/schtasks units
    "web_restart_seconds": 2,      # systemd RestartSec for crr-web.service after a crash
    # page timing/caps injected into page.html at serve time (audit 2026-07-31, P5)
    "confirm_arm_seconds": 4,      # danger-button confirm-arm window before it disarms
    "notice_seconds": 3,           # transient notice banner display duration
    "reload_delay_ms": 800,        # delay before a stale-page self-heal reload
    "diag_error_display_cap": 20,  # max previous-boot error lines rendered client-side
    # transcript scan bound (audit 2026-07-31, P5): a real model id always sits
    # within a few lines of the tail (measured on 3243 live transcripts:
    # p50=3, p99=37 lines back), but ~1 in 3 transcripts carry NO model at all
    # — so the model search stops here rather than reading a model-less
    # transcript in full on every poll. See transcript_source.read_tail_facts.
    "model_tail_lines": 200,
    # context-pressure badge (Slice A, F2; audit P5): fraction of a model's
    # (prior, unverified for most models) context window at which a session
    # is "tight" vs. expected to compact on revive. See
    # crr.core.context_pressure.pressure.
    "context_tight_fraction": 0.7,
    "context_compact_fraction": 1.0,
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
