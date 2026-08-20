"""Tailscale CLI adapter — reads `tailscale status`/`serve status` as JSON.

Mirrors crr/adapters/tmux.py: a class holding a timeout, pure command
builders, and tri-state wrappers that return None on missing binary /
timeout / OSError / nonzero exit / unparseable output (never raise). All
interpretation of the parsed JSON lives in pure core (crr.core.tailnet).

Note: `tailscale serve status --json` may exit nonzero or emit non-dict
output when serve is unconfigured — both collapse to None here, which the
core treats as "serve not live." That is the intended degrade.
"""

from __future__ import annotations

import json
import shutil
import subprocess


def _status_cmd() -> list[str]:
    return ["tailscale", "status", "--json"]


def _serve_status_cmd() -> list[str]:
    return ["tailscale", "serve", "status", "--json"]


class RealTailscale:
    def __init__(self, timeout: float) -> None:
        self._timeout = timeout

    def available(self) -> bool:
        return shutil.which("tailscale") is not None

    def _run_json(self, argv: list[str]) -> dict | None:
        if shutil.which("tailscale") is None:
            return None
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def status(self) -> dict | None:
        return self._run_json(_status_cmd())

    def serve_status(self) -> dict | None:
        return self._run_json(_serve_status_cmd())
