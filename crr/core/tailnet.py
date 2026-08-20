"""Pure selection/URL logic over parsed `tailscale status` JSON.

No subprocess, no I/O — the crr.adapters.tailscale adapter supplies the dicts.
Phase 3 (launcher) adds plan_launcher() here alongside self_dashboard_url().
"""

from __future__ import annotations


def _dashboard_url(dnsname: str) -> str:
    return "https://" + dnsname.rstrip(".") + "/"


def self_dashboard_url(status: dict | None, serve_status: dict | None) -> str | None:
    """This machine's dashboard URL, or None if it can't be served/resolved.

    None when: status unavailable; Self.DNSName missing/empty; or serve is not
    live (serve_status None or empty) — a node can be on the tailnet with
    nothing listening on 443, and a QR to a dead URL is worse than a hint.
    """
    if not status or not serve_status:
        return None
    dnsname = (status.get("Self") or {}).get("DNSName") or ""
    if not isinstance(dnsname, str) or not dnsname.strip():
        return None
    return _dashboard_url(dnsname.strip())
