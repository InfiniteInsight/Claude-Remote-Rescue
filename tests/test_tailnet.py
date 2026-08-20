from crr.core import tailnet

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
