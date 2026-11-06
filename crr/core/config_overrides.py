"""The crr-owned config overlay (``<state_dir>/config_overrides.json``).

``config.toml`` is the user's hand-owned file and crr never writes it (see
``crr/core/exclusions.py`` for why: no stdlib TOML writer, and a
regenerated file would destroy their comments). Settings changed through
crr are stored here instead and layered OVER the TOML:

    overlay (this file)  >  config.toml  >  DEFAULTS

Same shape as the sibling stores: ``{"v": 1, "values": {key: value}}``,
atomic writes, and a missing or unreadable file degrades to "no overlay"
so every command keeps running on ``config.toml`` plus the defaults.
``is_degraded()`` tells the caller the file EXISTS but could not be read,
so it can say so rather than silently ignoring the user's changes.

Five keys are NOT stored here (``OWNED_ELSEWHERE``): the dashboard already
keeps them in its own stores, and a second home would make two writers
disagree about one setting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crr.core import config as cfg
from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic

FILENAME = "config_overrides.json"

# Settings the dashboard's stores own (settings.json, exclusions.json).
OWNED_ELSEWHERE = frozenset({
    "remote_control_autokick",
    "tunnel_provider",
    "cloudflare_tunnel_name",
    "cloudflare_hostname",
    "discover_exclude_dirs",
})


def _valid(key: Any, value: Any) -> bool:
    if not isinstance(key, str) or key in OWNED_ELSEWHERE:
        return False
    try:
        cfg.check_value(key, value)
    except cfg.ConfigError:
        return False
    return True


class ConfigOverridesStore:
    """Read/write the crr-owned config overlay."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / FILENAME

    def _read_checked(self) -> tuple[dict[str, Any], bool]:
        """``(values, degraded)``. ``degraded`` is True only when the file
        EXISTS but could not be understood, never for a missing file. Bad
        individual values are dropped one at a time and do not degrade the
        rest of the file."""
        if not self._path.exists():
            return {}, False
        try:
            data = read_json_file(self._path)
        except (OSError, ValueError):
            return {}, True
        if not contracts.store_version_ok(data, contracts.CONFIG_OVERRIDES_STORE_VERSION):
            return {}, True
        values = data.get("values", {})
        if not isinstance(values, dict):
            return {}, True
        return {k: v for k, v in values.items() if _valid(k, v)}, False

    def read(self) -> dict[str, Any]:
        """The valid stored overrides, or ``{}`` when absent or unreadable."""
        return self._read_checked()[0]

    def is_degraded(self) -> bool:
        """True when the file exists but cannot be understood."""
        return self._read_checked()[1]

    def _write(self, values: dict[str, Any]) -> None:
        write_json_atomic(
            self._path,
            {"v": contracts.CONFIG_OVERRIDES_STORE_VERSION, "values": values},
        )

    def _refuse_if_degraded(self) -> None:
        """Never write over a file crr cannot fully read: it may hold values
        a newer crr or the user's hand edit put there. Reads still degrade."""
        if self.is_degraded():
            raise cfg.ConfigError(
                f"{self._path} cannot be read; fix or remove it before changing settings"
            )

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` for ``key``, or raise ``cfg.ConfigError`` for an
        unknown key, a mistyped value, a key owned by a dashboard store, or
        an overlay file that cannot be read (see ``_refuse_if_degraded``)."""
        if key in OWNED_ELSEWHERE:
            raise cfg.ConfigError(
                f"config '{key}' is managed from the dashboard, not the overlay"
            )
        cfg.check_value(key, value)
        self._refuse_if_degraded()
        values = self.read()
        values[key] = value
        self._write(values)

    def unset(self, key: str) -> bool:
        """Remove ``key``; True when there was something to remove. Raises
        ``cfg.ConfigError`` for an overlay file that cannot be read."""
        self._refuse_if_degraded()
        values = self.read()
        if key not in values:
            return False
        del values[key]
        self._write(values)
        return True
