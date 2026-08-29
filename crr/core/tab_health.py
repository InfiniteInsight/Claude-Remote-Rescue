"""Tab-spawn health — which launcher tier last opened a tab (spec 2026-08-29).

Pure core. crr opens a visible tab on WSL through Windows Terminal; when the
``wt.exe`` App Execution Alias is unusable the adapter falls through to
alternate launchers. This module remembers which tier last worked so
``crr doctor`` can say so, and formats that line. It records only outcomes
of spawn attempts that already happened — it never probes, because probing
wt.exe opens a GUI window.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic

FILENAME = "tab_health.json"

# Launcher tiers, best first. Values are persisted — do not rename.
TIER_WT = "wt"            # wt.exe from PATH (the App Execution Alias stub)
TIER_AUMID = "aumid"      # Start-Process shell:appsFolder\...!App (alias bypassed)
TIER_CONSOLE = "console"  # Start-Process wsl.exe (plain window, no Windows Terminal)
TIER_NONE = "none"        # every tier failed


class TabHealthStore:
    """Read/write the last tab-spawn outcome."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / FILENAME

    def read(self) -> dict[str, Any] | None:
        """The last record, or None when absent, corrupt, or a future version."""
        try:
            data = read_json_file(self._path)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if not contracts.store_version_ok(data, contracts.TAB_HEALTH_STORE_VERSION):
            return None
        return data

    def record(self, tier: str, detail: str = "", *, now: str, boot_id: str) -> None:
        write_json_atomic(self._path, {
            "v": contracts.TAB_HEALTH_STORE_VERSION,
            "tier": tier,
            "detail": detail,
            "ts": now,
            "boot_id": boot_id,
        })


LABEL = "tab spawn"

# Shown only for TIER_AUMID. Deliberately does NOT assert the alias is
# broken: wt_probe cannot tell a disabled alias from a context where wt.exe
# cannot exec (tmux, systemd), so a confident claim would sometimes be wrong.
ALIAS_NOTE = (
    "if you want the alias back: Settings -> Apps -> Advanced app settings "
    '-> App execution aliases -> turn on "Terminal (wt.exe)"'
)


def doctor_line(record: dict[str, Any] | None) -> tuple[str, bool | None, str]:
    """Render the tab-spawn health line as ``cli._check(label, ok, detail)`` args.

    ``ok`` is tri-state, matching doctor's renderer: True renders [ok  ],
    False renders [WARN], None renders the unknown state. The timestamp is
    always shown because this reports history, not a live probe — the user
    may have fixed things since.
    """
    if record is None:
        return LABEL, True, "not yet exercised"

    tier = record.get("tier")
    ts = record.get("ts", "unknown time")
    detail = record.get("detail", "")
    when = f"last attempt {ts}"

    if tier == TIER_WT:
        return LABEL, True, f"wt.exe — {when}"
    if tier == TIER_AUMID:
        return LABEL, True, (
            f"via the app package rather than the wt.exe alias; tabs are "
            f"opening normally — {when}. Nothing is broken in crr either "
            f"way; {ALIAS_NOTE}"
        )
    if tier == TIER_CONSOLE:
        return LABEL, True, (
            f"console fallback — Windows Terminal unavailable, tabs open in "
            f"a separate window — {when}"
        )
    if tier == TIER_NONE:
        return LABEL, False, f"no launcher worked: {detail} — {when}"
    return LABEL, None, f"unrecognized tab-spawn record — {when}"
