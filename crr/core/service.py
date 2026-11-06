"""What `crr service status` reports about crr's own systemd user units.

Pure core: no subprocess, no I/O. The systemd adapter probes the units and
hands back ``UnitState`` rows; ``crr.cli`` composes them with the backend,
the dashboard URL and the login flag into the contracted payload.
"""

from __future__ import annotations

from typing import NamedTuple, Sequence

from crr.core import contracts


class UnitState(NamedTuple):
    name: str        # what the user calls it: watchdog, dashboard, keep-awake
    unit: str        # the systemd unit behind it
    installed: bool  # a unit file is loaded
    enabled: bool    # starts on its own (at login/boot)
    active: bool     # running now


def overall_state(units: Sequence[UnitState]) -> str:
    """One word for the whole set; units that are not installed don't count.

    ``not-installed`` when none is installed, ``running`` when every installed
    unit is active, ``stopped`` when every installed unit is inactive,
    ``partial`` otherwise.
    """
    installed = [u for u in units if u.installed]
    if not installed:
        return "not-installed"
    if all(u.active for u in installed):
        return "running"
    if not any(u.active for u in installed):
        return "stopped"
    return "partial"


def status_payload(
    backend: str,
    reason: str,
    units: Sequence[UnitState],
    dashboard_url: str,
    dashboard_login: bool,
) -> dict:
    """The `crr service status --json` body (``contracts.SERVICE_*``)."""
    return {
        "contract": contracts.SERVICE_CONTRACT_VERSION,
        "backend": backend,
        "reason": reason,
        "state": overall_state(units),
        "services": [dict(zip(contracts.SERVICE_UNIT_FIELDS, u)) for u in units],
        "dashboard_url": dashboard_url,
        "dashboard_login": dashboard_login,
    }
