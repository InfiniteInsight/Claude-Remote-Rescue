"""Diagnostics payload assembly — "why did my session die" (pure core).

Parses journald's ``--list-boots -o json`` and assembles the versioned
/api/diagnostics payload. The subprocess calls and per-source timeout/
degrade handling live in the adapter + composition root; this module is
pure so it is testable with synthetic output.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Sequence

# macOS ``sysctl -n kern.boottime`` looks like ``{ sec = 1784723478, usec = 0
# } <date>``; the seconds field is the per-boot identity. Duplicated (not
# imported) from the boot-identity adapter to avoid an adapter→adapter edge.
_MAC_BOOTTIME_SEC_RE = re.compile(r"sec\s*=\s*(\d+)")


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


def parse_mac_boottime(sysctl_output: str) -> list[dict[str, Any]]:
    """Turn ``sysctl -n kern.boottime`` into a single current-boot record.

    macOS has no ``journalctl --list-boots`` equivalent, so ``boots`` on
    macOS is just the current boot (start time + the seconds field as its
    stable id). Returns ``[]`` if the output can't be parsed, so the caller
    degrades honestly rather than emitting a bogus record.
    """
    match = _MAC_BOOTTIME_SEC_RE.search(sysctl_output)
    if not match:
        return []
    sec = int(match.group(1))
    return [{
        "index": 0,
        "boot_id": str(sec),
        "start": datetime.fromtimestamp(sec, timezone.utc).isoformat(),
        "stop": "",
    }]


def filter_lines(text: str, terms: Sequence[str], cap: int) -> list[str]:
    """Return non-blank lines matching any of ``terms`` (case-insensitive), capped.

    The client-side analogue of journald's ``-g <pattern>``: macOS ``log
    show`` / ``pmset -g log`` have no built-in grep, so host-death signatures
    are filtered here from the raw output.
    """
    if not terms:
        return []
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    hits = [line for line in text.splitlines() if line.strip() and pattern.search(line)]
    return hits[:cap] if cap else hits


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
