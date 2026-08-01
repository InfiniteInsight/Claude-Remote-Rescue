"""Diagnostics payload assembly — "why did my session die" (pure core).

Parses journald's ``--list-boots -o json`` and assembles the versioned
/api/diagnostics payload. The subprocess calls and per-source timeout/
degrade handling live in the adapter + composition root; this module is
pure so it is testable with synthetic output.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Sequence

# The per-source degrade contract: a diagnostics sub-source that raises one of
# these is recorded in ``degraded`` rather than aborting the diagnosis. Shared
# by every diagnostics adapter so the "never raise per source" guarantee has
# one definition (adding a type here widens it everywhere, not in one copy).
DEGRADE_ERRORS = (subprocess.SubprocessError, OSError, RuntimeError, ValueError)

# macOS ``sysctl -n kern.boottime`` looks like ``{ sec = 1784723478, usec = 0
# } <date>``. The boot-identity adapter parses the same field for a different
# return shape; the one-line regex is intentionally restated here rather than
# creating a diagnostics→boot_identity coupling for a trivial pattern.
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
    params: dict,
    summary: list | None = None,
) -> dict[str, Any]:
    """Assemble and validate the /api/diagnostics payload.

    ``summary`` is the plain-English death verdict; when omitted it is derived
    from the events here so every caller gets it without duplicating the call.

    ``params`` is the generating caps/lookback/timeout the selected source
    actually queried with (audit P3/P5 — lineage travels with the data): a
    payload without it is unregenerable and unjudgeable later, since the
    priors it was produced under are gone the moment ``collect()`` returns.
    """
    from crr.core import contracts, explain

    if summary is None:
        summary = explain.summarize(host_events, prev_boot_errors)
    payload = {
        "contract": contracts.DIAGNOSTICS_CONTRACT_VERSION,
        "source": source,
        "summary": summary,
        "boots": boots,
        "prev_boot_errors": prev_boot_errors,
        "host_events": host_events,
        "degraded": degraded,
        "params": params,
    }
    contracts.validate_diagnostics_payload(payload)
    return payload
