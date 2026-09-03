"""Pure tunnel-provider selection (spec 2026-09-02).

Effective value = settings override ?? config default, field by field —
the autokick layering pattern. No I/O here: the cli resolves config and
the SettingsStore and passes plain values in. An unknown provider value
raises ValueError naming it (loud, not laundered): Config only
type-checks strings, so this is where the enum bites.
"""

from __future__ import annotations

from typing import Mapping, NamedTuple

TUNNEL_PROVIDERS = ("tailscale", "cloudflare", "none")


class TunnelSelection(NamedTuple):
    provider: str
    tunnel_name: str
    hostname: str
    origin: str  # "configured" | "override" — where provider came from


def select(
    config_provider: str,
    config_tunnel_name: str,
    config_hostname: str,
    override: Mapping[str, str | None] | None,
) -> TunnelSelection:
    override = override or {}
    provider = override.get("provider") or config_provider
    origin = "override" if override.get("provider") else "configured"
    if provider not in TUNNEL_PROVIDERS:
        raise ValueError(
            f"unknown tunnel provider {provider!r} — expected one of {TUNNEL_PROVIDERS}"
        )
    return TunnelSelection(
        provider=provider,
        tunnel_name=override.get("cloudflare_tunnel_name") or config_tunnel_name,
        hostname=override.get("cloudflare_hostname") or config_hostname,
        origin=origin,
    )
