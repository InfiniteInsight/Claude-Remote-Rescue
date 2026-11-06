"""crr.core.service: the pure model behind `crr service status` (#161)."""

import pytest

from crr.core import contracts, service
from crr.core.service import UnitState


def _u(name, installed=True, enabled=True, active=True):
    return UnitState(name, f"crr-{name}.service", installed, enabled, active)


@pytest.mark.parametrize("units, expected", [
    ([], "not-installed"),
    ([_u("a", installed=False, enabled=False, active=False)], "not-installed"),
    ([_u("a"), _u("b")], "running"),
    ([_u("a", active=False), _u("b", active=False)], "stopped"),
    ([_u("a"), _u("b", active=False)], "partial"),
    # a unit that is not installed does not count against the others
    ([_u("a"), _u("b", installed=False, enabled=False, active=False)], "running"),
    ([_u("a", active=False), _u("b", installed=False, enabled=False, active=False)], "stopped"),
])
def test_overall_state(units, expected):
    assert service.overall_state(units) == expected


def test_status_payload_is_a_valid_contract_body():
    units = [_u("watchdog"), _u("dashboard", active=False)]
    payload = service.status_payload(
        "systemd", "", units, "http://127.0.0.1:8377/", True)
    contracts.validate_service_payload(payload)
    assert payload["contract"] == contracts.SERVICE_CONTRACT_VERSION
    assert payload["state"] == "partial"
    assert payload["dashboard_login"] is True
    assert payload["services"][1] == {
        "name": "dashboard", "unit": "crr-dashboard.service",
        "installed": True, "enabled": True, "active": False}
    assert tuple(payload) == contracts.SERVICE_PAYLOAD_KEYS


def test_payload_key_order_names_dashboard_login_after_dashboard_url():
    assert contracts.SERVICE_PAYLOAD_KEYS == (
        "contract", "backend", "reason", "state", "services",
        "dashboard_url", "dashboard_login")
    assert contracts.SERVICE_UNIT_FIELDS == (
        "name", "unit", "installed", "enabled", "active")
