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

    def _read_raw(self) -> tuple[dict[str, Any], bool]:
        """``(values, degraded)`` with ``values`` exactly as stored, including
        keys this build does not know and values it would refuse. ``degraded``
        is True only when the file EXISTS but could not be understood, never
        for a missing file."""
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
        return dict(values), False

    def _read_checked(self) -> tuple[dict[str, Any], bool]:
        """``(values, degraded)``: ``_read_raw`` with each value this build
        cannot use dropped one at a time; a bad value does not degrade the
        rest of the file."""
        values, degraded = self._read_raw()
        return {k: v for k, v in values.items() if _valid(k, v)}, degraded

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

    def _raw_for_write(self) -> dict[str, Any]:
        """The stored values to modify and write back.

        Raw, not ``read()``'s filtered view: the file may hold keys a newer crr
        wrote, or values this build refuses, and one writer must never discard
        another's state (the settings store's ``_carry_*`` rule) — an older
        crr after a rollback would otherwise delete them on its first write.

        Never write over a file crr cannot read at all: it may hold values a
        newer crr or the user's hand edit put there. Reads still degrade.
        """
        values, degraded = self._read_raw()
        if degraded:
            raise cfg.ConfigError(
                f"{self._path} cannot be read; fix or remove it before changing settings"
            )
        return values

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` for ``key``, or raise ``cfg.ConfigError`` for an
        unknown key, a mistyped value, a key owned by a dashboard store, or
        an overlay file that cannot be read (see ``_raw_for_write``). Only
        ``key`` is validated; every other stored value is kept as it is."""
        if key in OWNED_ELSEWHERE:
            raise cfg.ConfigError(
                f"config '{key}' is managed from the dashboard, not the overlay"
            )
        cfg.check_value(key, value)
        values = self._raw_for_write()
        values[key] = value
        self._write(values)

    def unset(self, key: str) -> bool:
        """Remove ``key``; True when the file held it (even with a value this
        build ignores). Raises ``cfg.ConfigError`` for an overlay file that
        cannot be read."""
        values = self._raw_for_write()
        if key not in values:
            return False
        del values[key]
        self._write(values)
        return True
