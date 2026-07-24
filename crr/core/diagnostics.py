"""Diagnostics payload assembly — "why did my session die" (pure core).

Parses journald's ``--list-boots -o json`` and assembles the versioned
/api/diagnostics payload. The subprocess calls and per-source timeout/
degrade handling live in the adapter + composition root; this module is
pure so it is testable with synthetic output.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _iso_from_us(micros: Any) -> str:
    """journald timestamps are microseconds since the epoch."""
    if not isinstance(micros, (int, float)) or micros <= 0:
        return ""
    return datetime.fromtimestamp(micros / 1_000_000, timezone.utc).isoformat()


def parse_boots(json_output: str, cap: int) -> list[dict[str, Any]]:
    """Parse ``journalctl --list-boots -o json`` into the most recent ``cap`` boots."""
    try:
        data = json.loads(json_output)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    recent = data[-cap:] if cap else data
    boots = []
    for entry in recent:
        if not isinstance(entry, dict):
            continue
        boots.append({
            "index": entry.get("index"),
            "boot_id": entry.get("boot_id"),
            "start": _iso_from_us(entry.get("first_entry")),
            "stop": _iso_from_us(entry.get("last_entry")),
        })
    return boots


def build_payload(
    *,
    source: str,
    boots: list,
    prev_boot_errors: list,
    host_events: list,
    degraded: list,
) -> dict[str, Any]:
    """Assemble and validate the /api/diagnostics payload."""
    from crr.core import contracts

    payload = {
        "contract": contracts.DIAGNOSTICS_CONTRACT_VERSION,
        "source": source,
        "boots": boots,
        "prev_boot_errors": prev_boot_errors,
        "host_events": host_events,
        "degraded": degraded,
    }
    contracts.validate_diagnostics_payload(payload)
    return payload
