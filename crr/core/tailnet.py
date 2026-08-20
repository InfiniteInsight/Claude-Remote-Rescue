"""Pure selection/URL logic over parsed `tailscale status` JSON.

No subprocess, no I/O — the crr.adapters.tailscale adapter supplies the
dicts. Provides self_dashboard_url() (this machine's dashboard URL) and
plan_launcher() (the tagged-peer machine list for the launcher panel).
"""

from __future__ import annotations

from typing import NamedTuple


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


class MachineRow(NamedTuple):
    name: str
    url: str
    online: bool
    is_self: bool
    os: str


def plan_launcher(
    status: dict | None,
    *,
    tag: str,
    self_dnsname: str | None,
) -> list[MachineRow]:
    """Select tagged nodes from parsed tailscale status JSON.

    Returns [] when status is unavailable. Only nodes whose Tags list
    contains tag are included — Self and Peers alike.
    is_self is set when self_dnsname matches the node's DNSName.
    Sort: self-first, then online-first, then by name.
    """
    if status is None:
        return []

    rows: list[MachineRow] = []

    def _is_self(node: dict) -> bool:
        if not self_dnsname:
            return False
        dns = (node.get("DNSName") or "").rstrip(".")
        return dns == self_dnsname.rstrip(".")

    # Self — only if tagged
    self_node = status.get("Self") or {}
    if tag in self_node.get("Tags", []):
        rows.append(MachineRow(
            name=self_node.get("HostName", ""),
            url=_dashboard_url(self_node["DNSName"]),
            online=bool(self_node.get("Online")),
            is_self=_is_self(self_node),
            os=self_node.get("OS", ""),
        ))

    # Peers — only tagged ones
    for peer in (status.get("Peer") or {}).values():
        if tag not in peer.get("Tags", []):
            continue
        rows.append(MachineRow(
            name=peer.get("HostName", ""),
            url=_dashboard_url(peer["DNSName"]),
            online=bool(peer.get("Online")),
            is_self=_is_self(peer),
            os=peer.get("OS", ""),
        ))

    rows.sort(key=lambda r: (not r.is_self, not r.online, r.name))
    return rows
