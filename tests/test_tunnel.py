"""Pure tunnel-provider selection (spec 2026-09-02).

No I/O: config values and the settings override mapping are passed in;
the cli resolves both and calls select().
"""

import pytest

from crr.core import tunnel


def test_config_only_selection():
    sel = tunnel.select("tailscale", "", "", None)
    assert sel == tunnel.TunnelSelection("tailscale", "", "", "configured")


def test_override_wins_field_by_field():
    sel = tunnel.select(
        "tailscale", "cfg-name", "cfg.example.com",
        {"provider": "cloudflare", "cloudflare_tunnel_name": None,
         "cloudflare_hostname": "crr.example.com"},
    )
    assert sel.provider == "cloudflare"
    assert sel.tunnel_name == "cfg-name"        # None = no override
    assert sel.hostname == "crr.example.com"
    assert sel.origin == "override"


def test_origin_is_configured_when_override_has_no_provider():
    sel = tunnel.select("none", "", "", {"provider": None,
                                          "cloudflare_tunnel_name": None,
                                          "cloudflare_hostname": None})
    assert sel.provider == "none"
    assert sel.origin == "configured"


@pytest.mark.parametrize("bad", ["wireguard", "", "Tailscale"])
def test_unknown_provider_raises_naming_the_value(bad):
    with pytest.raises(ValueError, match=bad or "''"):
        tunnel.select(bad, "", "", None)


def test_unknown_override_provider_raises_too():
    with pytest.raises(ValueError):
        tunnel.select("tailscale", "", "", {"provider": "ngrok",
                                             "cloudflare_tunnel_name": None,
                                             "cloudflare_hostname": None})
