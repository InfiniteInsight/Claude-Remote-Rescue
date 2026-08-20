from crr.core import tailnet
from crr.core.tailnet import MachineRow, plan_launcher

_SERVE_LIVE = {"TCP": {"443": {"HTTPS": True}}}  # any non-empty dict = serve live


def test_url_from_self_dnsname_strips_trailing_dot():
    status = {"Self": {"DNSName": "lovelace.tail3af2d9.ts.net."}}
    assert tailnet.self_dashboard_url(status, _SERVE_LIVE) == \
        "https://lovelace.tail3af2d9.ts.net/"


def test_none_status_is_none():
    assert tailnet.self_dashboard_url(None, _SERVE_LIVE) is None


def test_missing_dnsname_is_none():
    assert tailnet.self_dashboard_url({"Self": {}}, _SERVE_LIVE) is None
    assert tailnet.self_dashboard_url({"Self": {"DNSName": ""}}, _SERVE_LIVE) is None


def test_serve_not_live_is_none():
    status = {"Self": {"DNSName": "lovelace.tail3af2d9.ts.net."}}
    assert tailnet.self_dashboard_url(status, None) is None
    assert tailnet.self_dashboard_url(status, {}) is None


# -- plan_launcher() --

# Self node's DNSName (used as self_dnsname parameter)
_SELF_DNS = "hedylamarr-1.tail3af2d9.ts.net."

_STATUS_WITH_PEERS = {
    "Self": {
        "HostName": "HedyLamarr",
        "DNSName": _SELF_DNS,
        "Online": True,
        # Self might not have Tags at all
    },
    "Peer": {
        "nodekey:abc123": {
            "HostName": "Lovelace",
            "DNSName": "lovelace.tail3af2d9.ts.net.",
            "Online": True,
            "Tags": ["tag:crr"],
        },
        "nodekey:def456": {
            "HostName": "Turing",
            "DNSName": "turing.tail3af2d9.ts.net.",
            "Online": False,
            "Tags": ["tag:crr", "tag:server"],
        },
        "nodekey:ghi789": {
            "HostName": "Babbage",
            "DNSName": "babbage.tail3af2d9.ts.net.",
            "Online": True,
            # No Tags key — untagged node
        },
    },
}

_STATUS_SELF_ONLY = {
    "Self": {
        "HostName": "HedyLamarr",
        "DNSName": _SELF_DNS,
        "Online": True,
    },
    "Peer": {},
}


def test_plan_launcher_none_status_returns_empty():
    assert plan_launcher(None, tag="tag:crr", self_dnsname=_SELF_DNS) == []


def test_plan_launcher_self_always_included():
    rows = plan_launcher(_STATUS_SELF_ONLY, tag="tag:crr", self_dnsname=_SELF_DNS)
    assert len(rows) == 1
    assert rows[0].is_self is True
    assert rows[0].name == "HedyLamarr"
    assert rows[0].url == "https://hedylamarr-1.tail3af2d9.ts.net/"
    assert rows[0].online is True


def test_plan_launcher_filters_tagged_peers():
    rows = plan_launcher(_STATUS_WITH_PEERS, tag="tag:crr", self_dnsname=_SELF_DNS)
    names = [r.name for r in rows]
    assert "Lovelace" in names
    assert "Turing" in names
    assert "Babbage" not in names  # untagged


def test_plan_launcher_url_from_dnsname_not_hostname():
    """DNSName (hedylamarr-1) differs from HostName (HedyLamarr) — URL must use DNSName."""
    rows = plan_launcher(_STATUS_WITH_PEERS, tag="tag:crr", self_dnsname=_SELF_DNS)
    self_row = [r for r in rows if r.is_self][0]
    assert self_row.url == "https://hedylamarr-1.tail3af2d9.ts.net/"
    assert self_row.name == "HedyLamarr"  # display from HostName


def test_plan_launcher_sort_self_first_online_first_then_name():
    rows = plan_launcher(_STATUS_WITH_PEERS, tag="tag:crr", self_dnsname=_SELF_DNS)
    # Self first
    assert rows[0].is_self is True
    # Among non-self: online before offline, then alphabetical
    non_self = rows[1:]
    assert non_self[0].name == "Lovelace"   # online
    assert non_self[1].name == "Turing"     # offline


def test_plan_launcher_online_offline_from_node():
    rows = plan_launcher(_STATUS_WITH_PEERS, tag="tag:crr", self_dnsname=_SELF_DNS)
    by_name = {r.name: r for r in rows}
    assert by_name["Lovelace"].online is True
    assert by_name["Turing"].online is False


def test_plan_launcher_no_self_dnsname_skips_self():
    """If self_dnsname is None (status unavailable for Self), no Self row."""
    rows = plan_launcher(_STATUS_WITH_PEERS, tag="tag:crr", self_dnsname=None)
    assert all(not r.is_self for r in rows)


def test_plan_launcher_self_dnsname_not_in_status():
    """self_dnsname doesn't match Self.DNSName — no is_self row."""
    rows = plan_launcher(_STATUS_WITH_PEERS, tag="tag:crr", self_dnsname="other.ts.net.")
    assert all(not r.is_self for r in rows)


def test_plan_launcher_returns_namedtuple():
    rows = plan_launcher(_STATUS_WITH_PEERS, tag="tag:crr", self_dnsname=_SELF_DNS)
    row = rows[0]
    assert isinstance(row, MachineRow)
    assert hasattr(row, "name")
    assert hasattr(row, "url")
    assert hasattr(row, "online")
    assert hasattr(row, "is_self")
