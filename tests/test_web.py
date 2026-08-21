"""Web dashboard request-handler tests (pure core, no http.server).

The security gate is the point of these tests: the Host allowlist (DNS-
rebinding defense), loopback-only intent, no-store self-heal headers, and
the versioned /api/sessions contract. The handler is a pure function of
(method, path, headers) so every branch is testable with fake requests.
"""

import json

import pytest

from crr.core import web


# --------------------------------------------------------------------------
# Host allowlist — the DNS-rebinding defense (exact + *.ts.net suffix)
# --------------------------------------------------------------------------

ALLOWED = {"localhost", "127.0.0.1", "hedylamarr"}
SUFFIXES = (".ts.net",)


@pytest.mark.parametrize("host", [
    "localhost",
    "127.0.0.1",
    "hedylamarr",
    "LOCALHOST",                       # case-insensitive
    "localhost:8377",                  # port stripped
    "host.tail3af2d9.ts.net",          # valid tailnet suffix
    "host.tail3af2d9.ts.net:443",      # suffix + port
])
def test_host_allowed_accepts_valid(host):
    assert web.host_allowed(host, ALLOWED, SUFFIXES) is True


@pytest.mark.parametrize("host", [
    "",                                # missing Host => reject
    "evil.com",
    "evil.ts.net.attacker.com",        # suffix must be at the END
    "ts.net",                          # bare apex is not *.ts.net
    "notlocalhost",
    "127.0.0.1.evil.com",
])
def test_host_allowed_rejects_invalid(host):
    assert web.host_allowed(host, ALLOWED, SUFFIXES) is False


# --------------------------------------------------------------------------
# Request routing + headers
# --------------------------------------------------------------------------

def _payload():
    # Minimal shape — auth fields omitted (routing tests, not contract conformance).
    return {"contract": 1, "sessions": []}


def _handle(method="GET", path="/", host="localhost", provider=None,
            body=b"", headers=None, action_provider=None,
            untracked_provider=None, discoverable_provider=None, sid_action_provider=None,
            recall_provider=None, exclusions_provider=None, exclusions_writer=None,
            settings_provider=None, settings_writer=None, qr_svg_provider=None,
            machines_provider=None, reauth_provider=None, reauth_code_provider=None,
            query=""):
    h = {"Host": host}
    if headers:
        h.update(headers)
    return web.handle_request(
        method, path, h, body,
        sessions_provider=provider or _payload,
        action_provider=action_provider,
        untracked_provider=untracked_provider,
        discoverable_provider=discoverable_provider,
        sid_action_provider=sid_action_provider,
        recall_provider=recall_provider,
        exclusions_provider=exclusions_provider,
        exclusions_writer=exclusions_writer,
        settings_provider=settings_provider,
        settings_writer=settings_writer,
        qr_svg_provider=qr_svg_provider,
        machines_provider=machines_provider,
        reauth_provider=reauth_provider,
        reauth_code_provider=reauth_code_provider,
        query=query,
        allowed_hosts=ALLOWED,
        allowed_suffixes=SUFFIXES,
    )


_RECALL_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_discoverable_endpoint_passes_paging_and_filter_to_provider():
    seen = []

    def disc(query, offset, limit):
        seen.append((query, offset, limit))
        return {"rows": [], "total": 2856, "filtered": 3, "offset": offset, "limit": limit}

    resp = _handle(path="/api/discoverable", query="q=payments&offset=40&limit=20",
                   discoverable_provider=disc)
    assert resp.status == 200
    assert seen == [("payments", 40, 20)]
    body = json.loads(resp.body)
    assert body["total"] == 2856 and body["filtered"] == 3  # both reported, no silent cap


def test_discoverable_endpoint_defaults_and_rejects_junk_paging():
    seen = []
    disc = lambda q, o, l: seen.append((q, o, l)) or {"rows": [], "total": 0, "filtered": 0,
                                                      "offset": o, "limit": l}
    _handle(path="/api/discoverable", discoverable_provider=disc)          # no params
    assert seen[-1][1] == 0 and seen[-1][2] > 0                            # sane defaults
    _handle(path="/api/discoverable", query="offset=-5&limit=abc", discoverable_provider=disc)
    assert seen[-1][1] >= 0 and seen[-1][2] > 0                            # junk -> defaults


def test_recall_endpoint_passes_query_and_sid_to_provider():
    seen = []

    def recall(q, sid):
        seen.append((q, sid))
        return {"matches": [{"role": "user", "text": "a fox", "session_id": _RECALL_SID}],
                "scanned": 1, "skipped": 0}

    resp = _handle(path="/api/recall", query=f"q=fox&sid={_RECALL_SID}", recall_provider=recall)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/json"
    assert seen == [("fox", _RECALL_SID)]
    assert json.loads(resp.body)["scanned"] == 1


def test_recall_without_sid_searches_globally():
    seen = []
    resp = _handle(path="/api/recall", query="q=fox",
                   recall_provider=lambda q, sid: seen.append((q, sid)) or {"matches": [], "scanned": 0, "skipped": 0})
    assert resp.status == 200
    assert seen == [("fox", None)]  # no sid -> global


def test_recall_empty_or_missing_query_is_400():
    calls = []
    prov = lambda q, sid: calls.append(1) or {"matches": [], "scanned": 0, "skipped": 0}
    assert _handle(path="/api/recall", query="q=", recall_provider=prov).status == 400
    assert _handle(path="/api/recall", query="q=%20%20", recall_provider=prov).status == 400  # whitespace
    assert _handle(path="/api/recall", query="", recall_provider=prov).status == 400  # missing key
    assert calls == []  # never reached the provider


def test_recall_bad_sid_is_400():
    prov = lambda q, sid: {"matches": [], "scanned": 0, "skipped": 0}
    assert _handle(path="/api/recall", query="q=fox&sid=not-a-uuid", recall_provider=prov).status == 400


def test_recall_404_without_provider():
    assert _handle(path="/api/recall", query="q=fox").status == 404


# --------------------------------------------------------------------------
# /api/exclusions — the admin section's read/write (C: dashboard-managed
# discovery exclusions; config.toml stays the user-owned baseline)
# --------------------------------------------------------------------------

def test_exclusions_get_returns_both_owners():
    resp = _handle(path="/api/exclusions",
                   exclusions_provider=lambda: {"configured": [".claude-mem"], "managed": ["scratch"]})
    assert resp.status == 200
    body = json.loads(resp.body)
    # both listed separately: the user must be able to tell which ones the
    # dashboard may edit and which come from their config.toml.
    assert body == {"configured": [".claude-mem"], "managed": ["scratch"]}


def test_exclusions_get_404_without_provider():
    assert _handle(path="/api/exclusions").status == 404


def test_exclusions_post_writes_and_returns_the_stored_list():
    seen = []
    resp = _handle(method="POST", path="/api/exclusions", headers=_JSON,
                   body=b'{"dirs": ["scratch", " tmp "]}',
                   exclusions_writer=lambda dirs: seen.append(dirs) or {
                       "configured": [".claude-mem"], "managed": ["scratch", "tmp"]})
    assert resp.status == 200
    assert seen == [["scratch", " tmp "]]
    assert json.loads(resp.body)["managed"] == ["scratch", "tmp"]


def test_exclusions_post_requires_json_content_type():
    # Same CSRF posture as /api/action — this one WRITES TO DISK, so the
    # gate matters at least as much.
    resp = _handle(method="POST", path="/api/exclusions",
                   headers={"Content-Type": "text/plain"}, body=b'{"dirs": []}',
                   exclusions_writer=lambda dirs: {})
    assert resp.status == 415


def test_exclusions_post_rejects_a_bad_payload():
    w = lambda dirs: {}
    assert _handle(method="POST", path="/api/exclusions", headers=_JSON,
                   body=b'not json', exclusions_writer=w).status == 400
    assert _handle(method="POST", path="/api/exclusions", headers=_JSON,
                   body=b'{"dirs": "nope"}', exclusions_writer=w).status == 400


def test_exclusions_post_surfaces_a_writer_rejection_as_400():
    def writer(dirs):
        raise ValueError("too many exclusions (max 100)")

    resp = _handle(method="POST", path="/api/exclusions", headers=_JSON,
                   body=b'{"dirs": ["x"]}', exclusions_writer=writer)
    assert resp.status == 400
    assert b"too many" in resp.body


def test_settings_get_404_without_provider():
    assert _handle(path="/api/settings").status == 404


def test_settings_get_returns_global_value_and_degraded_flag():
    resp = _handle(path="/api/settings", settings_provider=lambda: {
        "autokick": False, "resolved": False, "config_default": True, "degraded": False,
    })
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"autokick": False, "resolved": False, "config_default": True, "degraded": False}


def test_settings_get_surfaces_degraded_so_the_phone_knows_why():
    # A degraded settings store means the watchdog auto-kicks NOTHING
    # (fail-closed, Slice 2) regardless of what `resolved` says here — the
    # modal must be able to tell the user that, not just show a number.
    resp = _handle(path="/api/settings", settings_provider=lambda: {
        "autokick": None, "resolved": True, "config_default": True, "degraded": True,
    })
    assert json.loads(resp.body)["degraded"] is True


def test_settings_post_writes_and_returns_the_stored_value():
    seen = []
    resp = _handle(method="POST", path="/api/settings", headers=_JSON,
                   body=b'{"autokick": false}',
                   settings_writer=lambda v: seen.append(v) or {
                       "autokick": False, "resolved": False, "config_default": True, "degraded": False})
    assert resp.status == 200
    assert seen == [False]
    assert json.loads(resp.body)["resolved"] is False


def test_settings_post_null_clears_the_override():
    seen = []
    resp = _handle(method="POST", path="/api/settings", headers=_JSON,
                   body=b'{"autokick": null}',
                   settings_writer=lambda v: seen.append(v) or {
                       "autokick": None, "resolved": True, "config_default": True, "degraded": False})
    assert resp.status == 200
    assert seen == [None]


def test_settings_post_requires_json_content_type():
    resp = _handle(method="POST", path="/api/settings",
                   headers={"Content-Type": "text/plain"}, body=b'{"autokick": true}',
                   settings_writer=lambda v: {})
    assert resp.status == 415


def test_settings_post_rejects_a_bad_payload():
    w = lambda v: {}
    assert _handle(method="POST", path="/api/settings", headers=_JSON,
                   body=b'not json', settings_writer=w).status == 400
    assert _handle(method="POST", path="/api/settings", headers=_JSON,
                   body=b'{}', settings_writer=w).status == 400
    assert _handle(method="POST", path="/api/settings", headers=_JSON,
                   body=b'{"nope": true}', settings_writer=w).status == 400


def test_settings_post_surfaces_a_writer_rejection_as_400():
    def writer(value):
        raise ValueError("autokick must be a bool or None")

    resp = _handle(method="POST", path="/api/settings", headers=_JSON,
                   body=b'{"autokick": "yes"}', settings_writer=writer)
    assert resp.status == 400
    assert b"bool" in resp.body


def test_settings_post_missing_writer_is_503():
    resp = _handle(method="POST", path="/api/settings", headers=_JSON,
                   body=b'{"autokick": true}')
    assert resp.status == 503


def test_disallowed_host_is_403_before_anything_else():
    resp = _handle(host="evil.com", path="/api/sessions")
    assert resp.status == 403


def test_all_responses_are_no_store():
    # The self-heal depends on the browser never caching the page or APIs.
    for path in ("/", "/api/sessions", "/api/version"):
        resp = _handle(path=path)
        assert resp.headers.get("Cache-Control") == "no-store"


def test_get_root_serves_html_page_with_version_substituted():
    resp = _handle(path="/")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/html")
    body = resp.body.decode()
    assert "@PAGE_VERSION@" not in body            # placeholder substituted
    assert str(web.PAGE_VERSION) in body


def test_get_sessions_returns_the_contract_payload():
    resp = _handle(path="/api/sessions", provider=lambda: {"contract": 1, "sessions": [{"pid": 7}]})
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/json"
    assert json.loads(resp.body)["sessions"] == [{"pid": 7}]


def test_get_version_matches_page_version():
    resp = _handle(path="/api/version")
    assert json.loads(resp.body) == {"version": web.PAGE_VERSION}


def test_diagnostics_endpoint_uses_provider_lazily():
    calls = []
    def diag():
        calls.append(1)
        return {"contract": 1, "source": "journald", "degraded": []}
    resp = web.handle_request(
        "GET", "/api/diagnostics", {"Host": "localhost"},
        sessions_provider=_payload, diagnostics_provider=diag,
        allowed_hosts=ALLOWED, allowed_suffixes=SUFFIXES,
    )
    assert resp.status == 200
    assert json.loads(resp.body)["source"] == "journald"
    assert calls == [1]  # only called when the endpoint is hit


def test_diagnostics_endpoint_404_without_provider():
    assert _handle(path="/api/diagnostics").status == 404


# --------------------------------------------------------------------------
# GET /api/untracked — the last N untracked/detmuxed archive records (C2)
# --------------------------------------------------------------------------

_UNTRACKED_ITEM = {
    "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "sid8": "8a1b2c3d",
    "cwd": "/home/u/proj",
    "archived_at": "2026-08-01T00:00:00+00:00",
    "last_prompt": "the last thing I typed",
}


def test_untracked_endpoint_uses_provider_lazily():
    calls = []

    def untracked(query, offset, limit):
        calls.append(1)
        return {"rows": [_UNTRACKED_ITEM], "total": 1, "filtered": 1,
                "offset": offset, "limit": limit}

    resp = _handle(path="/api/untracked", untracked_provider=untracked)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/json"
    body = json.loads(resp.body)
    assert body["rows"] == [_UNTRACKED_ITEM]
    # last_prompt IS present now: cli._untracked_view reads it from the
    # untracked session's on-disk transcript (parity with the discoverable
    # panel), so the retrack panel can show what each candidate was about.
    assert set(body["rows"][0]) == {"session_id", "sid8", "cwd", "archived_at", "last_prompt"}
    assert calls == [1]  # only called when the endpoint is hit


def test_untracked_endpoint_pages_and_filters_like_discoverable():
    seen = []
    prov = lambda q, o, l: seen.append((q, o, l)) or {"rows": [], "total": 30, "filtered": 2,
                                                     "offset": o, "limit": l}
    resp = _handle(path="/api/untracked", query="q=proj&offset=20&limit=20",
                   untracked_provider=prov)
    assert seen == [("proj", 20, 20)]
    body = json.loads(resp.body)
    assert body["total"] == 30 and body["filtered"] == 2   # no silent cap


def test_untracked_endpoint_404_without_provider():
    assert _handle(path="/api/untracked").status == 404


# --------------------------------------------------------------------------
# GET /api/discoverable — untracked transcripts (T-C, C3)
# --------------------------------------------------------------------------

_DISCOVERABLE_ITEM = {
    "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "sid8": "8a1b2c3d",
    "cwd": "/home/u/proj",
    "last_active": "2026-08-01T00:00:00+00:00",
    "transcript_bytes": 123,
    "last_prompt": "hi",
}


def test_discoverable_endpoint_uses_provider_lazily():
    calls = []

    def discoverable(query, offset, limit):
        calls.append(1)
        return {"rows": [_DISCOVERABLE_ITEM], "total": 1, "filtered": 1,
                "offset": offset, "limit": limit}

    resp = _handle(path="/api/discoverable", discoverable_provider=discoverable)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/json"
    body = json.loads(resp.body)
    assert body["rows"] == [_DISCOVERABLE_ITEM]
    assert set(body["rows"][0]) == {
        "session_id", "sid8", "cwd", "last_active", "transcript_bytes", "last_prompt",
    }
    assert calls == [1]  # only called when the endpoint is hit


def test_discoverable_endpoint_404_without_provider():
    assert _handle(path="/api/discoverable").status == 404


def test_unknown_path_is_404():
    assert _handle(path="/nope").status == 404


def test_unsupported_method_is_405():
    assert _handle(method="DELETE", path="/").status == 405


# --------------------------------------------------------------------------
# POST /api/action — session ops with the CSRF/validation gate
# --------------------------------------------------------------------------

_JSON = {"Content-Type": "application/json"}


def _post(payload=None, host="localhost", headers=None, action_provider=None, raw=None):
    body = raw if raw is not None else json.dumps(payload or {}).encode()
    return _handle(method="POST", path="/api/action", host=host,
                   body=body, headers=headers if headers is not None else _JSON,
                   action_provider=action_provider)


def test_post_action_dispatches_and_returns_result():
    seen = {}
    def act(op, pid):
        seen["call"] = (op, pid)
        return True, f"reopened {pid}", False
    resp = _post({"op": "reopen", "pid": 42}, action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("reopen", 42)
    assert json.loads(resp.body) == {"contract": web.contracts.ACTION_CONTRACT_VERSION, "ok": True,
        "message": "reopened 42", "degraded": False}


def test_post_action_gate_refusal_is_409():
    resp = _post({"op": "dismiss", "pid": 42}, action_provider=lambda o, p: (False, "is live", False))
    assert resp.status == 409
    assert json.loads(resp.body)["ok"] is False


def test_post_without_json_content_type_is_415():
    resp = _post({"op": "remove", "pid": 42}, headers={}, action_provider=lambda o, p: (True, "ok", False))
    assert resp.status == 415


def test_post_bad_json_is_400():
    resp = _post(raw=b"{not json", action_provider=lambda o, p: (True, "ok", False))
    assert resp.status == 400


@pytest.mark.parametrize("payload", [
    {"op": "nuke", "pid": 42},       # unknown op
    {"op": "reopen", "pid": "42"},   # pid not an int
    {"op": "reopen", "pid": -1},     # non-positive
    {"op": "reopen", "pid": True},   # bool is not a real pid
    {"op": "reopen"},                # missing pid
    {"pid": 42},                     # missing op
])
def test_post_invalid_op_or_pid_is_400(payload):
    resp = _post(payload, action_provider=lambda o, p: (True, "should-not-run", False))
    assert resp.status == 400


def test_post_disallowed_host_is_403_before_dispatch():
    called = []
    resp = _post({"op": "remove", "pid": 42}, host="evil.com",
                 action_provider=lambda o, p: called.append(1) or (True, "ok", False))
    assert resp.status == 403
    assert called == []  # never dispatched


def test_actions_include_kick_and_close():
    from crr.core import web
    assert "kick" in web.ACTIONS
    assert "close" in web.ACTIONS


def test_actions_include_untrack():
    from crr.core import web
    assert "untrack" in web.ACTIONS


def test_actions_include_detmux():
    # Deprecated alias — the dashboard button now posts "untrack", but the
    # server must keep accepting "detmux" for back-compat.
    from crr.core import web
    assert "detmux" in web.ACTIONS


def test_actions_include_untmux():
    from crr.core import web
    assert "untmux" in web.ACTIONS


def test_post_kick_is_accepted_and_dispatched():
    from crr.core import web
    seen = {}
    def action_provider(op, pid):
        seen["op"], seen["pid"] = op, pid
        return True, "kicked 5 (resuming the same conversation)", False
    resp = web.handle_request(
        "POST", "/api/action",
        {"Host": "localhost", "Content-Type": "application/json"},
        b'{"op":"kick","pid":5}',
        sessions_provider=lambda: {"contract": 2, "sessions": []},
        action_provider=action_provider,
        allowed_hosts={"localhost"}, allowed_suffixes=(),
    )
    assert resp.status == 200
    assert seen == {"op": "kick", "pid": 5}


def test_post_untrack_is_accepted_and_dispatched():
    seen = {}
    def act(op, pid):
        seen["call"] = (op, pid)
        return True, "de-tmuxed 42: attached crr-8a1b2c3d in a tab; crr no longer manages it", False
    resp = _post({"op": "untrack", "pid": 42}, action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("untrack", 42)


def test_post_detmux_is_accepted_and_dispatched():
    # Deprecated alias must still round-trip through /api/action.
    seen = {}
    def act(op, pid):
        seen["call"] = (op, pid)
        return True, "de-tmuxed 42: attached crr-8a1b2c3d in a tab; crr no longer manages it", False
    resp = _post({"op": "detmux", "pid": 42}, action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("detmux", 42)
    assert json.loads(resp.body) == {
        "contract": web.contracts.ACTION_CONTRACT_VERSION,
        "ok": True,
        "message": "de-tmuxed 42: attached crr-8a1b2c3d in a tab; crr no longer manages it",
        "degraded": False,
    }


def test_post_untmux_is_accepted_and_dispatched():
    seen = {}
    def act(op, pid):
        seen["call"] = (op, pid)
        return True, "un-tmuxed 42: claude --resume in a new tab; crr no longer manages it", False
    resp = _post({"op": "untmux", "pid": 42}, action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("untmux", 42)
    assert json.loads(resp.body) == {
        "contract": web.contracts.ACTION_CONTRACT_VERSION,
        "ok": True,
        "message": "un-tmuxed 42: claude --resume in a new tab; crr no longer manages it",
        "degraded": False,
    }


def test_post_to_non_action_path_is_404():
    resp = _handle(method="POST", path="/api/other", body=b"{}", headers=_JSON)
    assert resp.status == 404


def test_post_action_still_rejects_a_bad_pid_unchanged():
    # /api/sid-action is a SEPARATE route (C2) — must not weaken the
    # existing pid-keyed /api/action's strict validation.
    resp = _post({"op": "reopen", "pid": "not-an-int"},
                 action_provider=lambda o, p: (True, "should-not-run", False))
    assert resp.status == 400


# --------------------------------------------------------------------------
# POST /api/sid-action — sid-keyed ops (C2), separate from /api/action
# --------------------------------------------------------------------------

_VALID_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _post_sid(payload=None, host="localhost", headers=None, sid_action_provider=None, raw=None):
    body = raw if raw is not None else json.dumps(payload or {}).encode()
    return _handle(method="POST", path="/api/sid-action", host=host,
                   body=body, headers=headers if headers is not None else _JSON,
                   sid_action_provider=sid_action_provider)


def test_sid_actions_include_retrack():
    assert "retrack" in web.SID_ACTIONS


def test_sid_actions_include_adopt():
    assert "adopt" in web.SID_ACTIONS


def test_sid_actions_include_autokick_on_and_off():
    assert "autokick-on" in web.SID_ACTIONS
    assert "autokick-off" in web.SID_ACTIONS


def test_post_sid_action_autokick_on_dispatches_and_returns_result():
    seen = {}

    def act(op, sid):
        seen["call"] = (op, sid)
        return True, "auto-kick enabled for this session", False

    resp = _post_sid({"op": "autokick-on", "sid": _VALID_SID}, sid_action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("autokick-on", _VALID_SID)
    assert json.loads(resp.body)["ok"] is True


def test_post_sid_action_autokick_off_dispatches_and_returns_result():
    seen = {}

    def act(op, sid):
        seen["call"] = (op, sid)
        return True, "auto-kick disabled for this session", False

    resp = _post_sid({"op": "autokick-off", "sid": _VALID_SID}, sid_action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("autokick-off", _VALID_SID)
    assert json.loads(resp.body)["ok"] is True


def test_post_sid_action_autokick_rejects_non_uuid_sid():
    resp = _post_sid({"op": "autokick-on", "sid": "not-a-uuid"},
                     sid_action_provider=lambda o, s: (True, "should-not-run"))
    assert resp.status == 400


def test_post_sid_action_adopt_dispatches_and_returns_result():
    seen = {}

    def act(op, sid):
        seen["call"] = (op, sid)
        return True, f"adopted {sid[:8]} — now tracked as recoverable", False

    resp = _post_sid({"op": "adopt", "sid": _VALID_SID}, sid_action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("adopt", _VALID_SID)
    assert json.loads(resp.body) == {"contract": web.contracts.ACTION_CONTRACT_VERSION, "ok": True,
        "message": f"adopted {_VALID_SID[:8]} — now tracked as recoverable", "degraded": False}


def test_post_sid_action_adopt_gate_refusal_is_409():
    resp = _post_sid({"op": "adopt", "sid": _VALID_SID},
                     sid_action_provider=lambda o, s: (False, "not discoverable", False))
    assert resp.status == 409
    assert json.loads(resp.body)["ok"] is False


def test_post_sid_action_dispatches_and_returns_result():
    seen = {}

    def act(op, sid):
        seen["call"] = (op, sid)
        return True, f"retracked {sid[:8]}", False

    resp = _post_sid({"op": "retrack", "sid": _VALID_SID}, sid_action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("retrack", _VALID_SID)
    assert json.loads(resp.body) == {"contract": web.contracts.ACTION_CONTRACT_VERSION, "ok": True,
        "message": f"retracked {_VALID_SID[:8]}", "degraded": False}


def test_post_sid_action_gate_refusal_is_409():
    resp = _post_sid({"op": "retrack", "sid": _VALID_SID},
                     sid_action_provider=lambda o, s: (False, "no archived session", False))
    assert resp.status == 409
    assert json.loads(resp.body)["ok"] is False


def test_post_sid_action_rejects_unknown_op():
    resp = _post_sid({"op": "nuke", "sid": _VALID_SID},
                     sid_action_provider=lambda o, s: (True, "should-not-run"))
    assert resp.status == 400


@pytest.mark.parametrize("sid", [
    "not-a-uuid",
    "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d\n",  # trailing newline must not slip through
    123,
    None,
])
def test_post_sid_action_rejects_a_non_uuid_sid(sid):
    resp = _post_sid({"op": "retrack", "sid": sid},
                     sid_action_provider=lambda o, s: (True, "should-not-run"))
    assert resp.status == 400


def test_post_sid_action_rejects_missing_sid():
    resp = _post_sid({"op": "retrack"}, sid_action_provider=lambda o, s: (True, "should-not-run"))
    assert resp.status == 400


def test_post_sid_action_without_json_content_type_is_415():
    resp = _post_sid({"op": "retrack", "sid": _VALID_SID}, headers={},
                     sid_action_provider=lambda o, s: (True, "ok"))
    assert resp.status == 415


def test_post_sid_action_bad_json_is_400():
    resp = _post_sid(raw=b"{not json", sid_action_provider=lambda o, s: (True, "ok"))
    assert resp.status == 400


def test_post_sid_action_missing_provider_is_503():
    resp = _post_sid({"op": "retrack", "sid": _VALID_SID})
    assert resp.status == 503


def test_post_sid_action_disallowed_host_is_403_before_dispatch():
    called = []
    resp = _post_sid({"op": "retrack", "sid": _VALID_SID}, host="evil.com",
                     sid_action_provider=lambda o, s: called.append(1) or (True, "ok"))
    assert resp.status == 403
    assert called == []


def test_options_preflight_is_rejected():
    # The CSRF defense relies on the forced preflight failing. There is no
    # do_OPTIONS handler; guard that a future one can't silently reopen it.
    assert _handle(method="OPTIONS", path="/api/action").status == 405


# --------------------------------------------------------------------------
# Poll/version intervals: config, not magic numbers (audit P5).
# --------------------------------------------------------------------------

def test_render_page_substitutes_poll_intervals_from_defaults():
    """[audit P5] page intervals must be config, not magic numbers."""
    page = web.render_page()
    assert "@POLL_MS@" not in page and "@VERSION_MS@" not in page
    assert "var POLL_MS = 5000;" in page          # 5 s default * 1000
    assert "var VERSION_MS = 30000;" in page      # 30 s default * 1000


def test_render_page_honors_configured_intervals():
    page = web.render_page(poll_seconds=7, version_check_seconds=60)
    assert "var POLL_MS = 7000;" in page
    assert "var VERSION_MS = 60000;" in page


def test_handle_request_serves_configured_intervals():
    resp = web.handle_request(
        "GET", "/", {"Host": "127.0.0.1"},
        sessions_provider=lambda: {}, allowed_hosts={"127.0.0.1"},
        allowed_suffixes=(), poll_seconds=7, version_check_seconds=60,
    )
    assert b"var POLL_MS = 7000;" in resp.body


# --------------------------------------------------------------------------
# Confirm-arm / notice / stale-reload / diag-error-cap: config, not magic
# numbers (audit P5, findings F2/F3/F4). Mirrors the poll_seconds pattern.
# --------------------------------------------------------------------------

def test_render_page_substitutes_timing_and_cap_from_defaults():
    page = web.render_page()
    assert "@CONFIRM_ARM_MS@" not in page
    assert "@NOTICE_MS@" not in page
    assert "@RELOAD_DELAY_MS@" not in page
    assert "@DIAG_ERR_CAP@" not in page
    assert "var CONFIRM_ARM_MS = 4000;" in page   # 4 s default * 1000
    assert "var NOTICE_MS = 3000;" in page        # 3 s default * 1000
    assert "var RELOAD_DELAY_MS = 800;" in page   # already ms
    assert "var DIAG_ERR_CAP = 20;" in page        # a count, not a time


def test_render_page_honors_configured_timing_and_cap():
    page = web.render_page(
        confirm_arm_seconds=9, notice_seconds=2,
        reload_delay_ms=500, diag_error_display_cap=5,
    )
    assert "var CONFIRM_ARM_MS = 9000;" in page
    assert "var NOTICE_MS = 2000;" in page
    assert "var RELOAD_DELAY_MS = 500;" in page
    assert "var DIAG_ERR_CAP = 5;" in page


def test_handle_request_serves_configured_timing_and_cap():
    resp = web.handle_request(
        "GET", "/", {"Host": "127.0.0.1"},
        sessions_provider=lambda: {}, allowed_hosts={"127.0.0.1"},
        allowed_suffixes=(),
        confirm_arm_seconds=9, notice_seconds=2,
        reload_delay_ms=500, diag_error_display_cap=5,
    )
    assert b"var CONFIRM_ARM_MS = 9000;" in resp.body
    assert b"var DIAG_ERR_CAP = 5;" in resp.body


# --------------------------------------------------------------------------
# node --check gate: every <script> in the served page must parse.
# --------------------------------------------------------------------------

def test_page_discoverable_is_a_modal_with_paging_and_filter():
    page = web.render_page()
    assert 'id="disc-modal"' in page          # modal shell
    assert 'id="disc-filter"' in page         # filter input
    assert 'id="disc-prev"' in page and 'id="disc-next"' in page   # pagination
    assert "discState" in page                # {query, offset} paging state
    assert "offset=" in page and "limit=" in page                  # server-side paging
    # filter placeholder must say WHAT it filters (it is not full-text search)
    assert "directory or id" in page


def test_page_discoverable_warns_when_a_row_is_still_running():
    """The duplicate hazard: adopting a conversation that is still live starts
    a SECOND claude on it. The row must say so and steer to Take over."""
    page = web.render_page()
    assert "r.running" in page
    assert "running (untracked)" in page
    # the label must say crr is NOT managing it — "running now" alone reads
    # like "already tracked", which is the opposite of the truth.
    assert "crr is not tracking" in page


def test_page_discoverable_reports_total_and_filtered_counts():
    # no silent caps: a paged/filtered view must never read as "that's all"
    page = web.render_page()
    assert ".total" in page
    assert ".filtered" in page


def test_page_buttons_have_explanatory_tooltips():
    """Every action button explains itself on hover — the labels alone
    ('Kick', 'Un-tmux', 'Adopt') don't tell a new user what will happen."""
    page = web.render_page()
    assert "BUTTON_HELP" in page
    for label in ("Reopen", "Restore", "Dismiss", "Remove", "Kick", "Close",
                  "Untrack", "Un-tmux", "Search", "Retrack", "Adopt", "Take over"):
        assert '"' + label + '":' in page, label
    # the helper actually applies them
    assert "BUTTON_HELP[" in page


def test_page_panel_toggles_have_tooltips():
    page = web.render_page()
    for el_id in ("diag-btn", "untracked-btn", "discoverable-btn", "recall-all-btn"):
        # each toggle carries a title= in the markup
        assert el_id in page
    assert page.count("title=") >= 4


def test_page_cards_label_every_field():
    """UX: bare values ('verified', a naked uuid8, a relative time) don't tell
    a new user what they are. Every card field carries a dim inline label."""
    page = web.render_page()
    for label in ('"pid "', '"sid "', '"sid:"', '"dir"', '"last"', '"model"', '"you said"'):
        assert label in page, label
    assert "fieldLabel" in page  # the shared label-element helper


def test_page_legend_explains_sid_provenance():
    # injected/guessed/verified is load-bearing (it downgrades duplicate
    # warnings) — the legend must say what the three values mean.
    page = web.render_page()
    assert "injected" in page
    assert "guessed" in page
    assert "verified" in page


def test_page_shows_claudes_preceding_reply():
    page = web.render_page()
    assert "s.last_reply" in page
    assert 'fieldLabel("claude")' in page


def test_page_duplicate_edge_colour_is_per_group():
    """Every duplicate used to share one hardcoded hue, so unrelated pairs
    looked related. The edge colour now derives from duplicate_group."""
    page = web.render_page()
    assert "colorFor(s.duplicate_group)" in page
    # uncertain still overrides to amber — the weaker claim wins
    assert ".card.dup-uncertain { border-left-color: #e0a53b !important; }" in page


def test_page_stacks_duplicate_cards_with_a_fan_out_toggle():
    page = web.render_page()
    assert "function renderStacked(" in page
    assert "dup-stack" in page and "dup-toggle" in page
    assert "expandedDups" in page          # open/closed survives the 5s poll
    assert "function stackTop(" in page    # the actionable card sits on top


def test_worktree_sessions_group_under_their_parent_repo():
    # #31: a worktree checkout is a side branch — tagged with its name and
    # demoted into a per-repo group below the main threads in the flat view.
    page = web.load_page()
    assert "function worktreeInfo(cwd)" in page
    assert "worktrees/" in page  # the marker the split keys on
    assert "worktree-badge" in page
    assert "worktree-head" in page


def test_notice_can_be_dismissed_and_copies_the_attach_command():
    # A degraded "no tab" reopen returns a sticky notice carrying
    # `tmux attach -t <name>`; it must be dismissable (×) and offer a Copy
    # button beside the command, not sit forever as unselectable text.
    page = web.load_page()
    assert "notice-close" in page
    assert "notice-copy" in page
    assert 'x.textContent = "×"' in page
    assert "/tmux attach -t [\\w-]+/" in page  # the command it lifts out to copy
    assert "navigator.clipboard" in page


def test_page_version_is_60():
    """v60: Auth expiry badge + reauth modal (header badge for
    expiring/expired/reauth-in-progress auth_state, plus the reauth modal
    wired to /api/reauth and /api/reauth-code).
    (v59: Reopen button on LIVE cards with tmux_session.
    (v58: Machines panel — tagged-only, OS field, "Tailnet Members" label.
    (v57: Machines panel — tag:crr peer list with on-tailnet badge.
    (v56: PWA installability — manifest link, apple-touch-icon, iOS meta
    tags, and service worker registration in page.html's head/script.
    (v55: style #adddev-box (padding/border/background, centered QR) and
    retry the /qr.svg fetch on every open instead of only the first click,
    so a 404 before serve comes up isn't sticky (final review findings).
    (v54: "Add a device" QR affordance (/qr.svg)
    (v53: notices are dismissable (×) and copy their attach command
    (v52: worktree sessions grouped under their parent repo (#31)
    (v51: discoverable worktree rows collapse into one expandable row (#34)
    (v50: an "attached" badge distinguishes a reopened parked card from one
    still merely restored (#32)
    (v49: conflict warning forces a choice (#48)
    (v48: header reachability summary + a "not connected" filter
    (v47: the card reports whether the phone can reach this session, from
    Claude Code's own connection state (spec 2026-08-09, Phases 1-3)
    (v46 gave parked cards Kick/Close, #58)."""
    assert web.PAGE_VERSION == 60


def test_page_renders_the_parked_state():
    page = web.load_page()
    assert "k-parked" in page
    assert "restored" in page
    # The colour rule alone is not a dot: `content`/`display`/width/height
    # come from the scoped `#key ...::before` selector list, so a term left
    # out of it renders with no dot at all.
    assert "#key .k-parked::before" in page


def test_key_explains_the_parked_state():
    page = web.load_page()
    key = page[page.index('id="key"'):page.index('id="key"') + 4000]
    assert "restored" in key


def test_parked_cards_get_their_own_action_set():
    # [#58] Was `..._the_crashed_action_set`. A parked session is no longer
    # CRASHED operationally — the journal is re-keyed onto the claude in the
    # pane — so it gets its own arm with the running-session controls rather
    # than borrowing the crashed one, where Kick/Close were unavailable and
    # Dismiss would only ever refuse.
    page = web.load_page()
    assert 'else if (s.state === "parked")' in page


def test_live_cards_get_a_reopen_button_when_tmux_session_present():
    # v59: LIVE sessions with a tmux_session can be re-attached to (Task 1
    # extended ops.reopen to handle this server-side). The button must only
    # appear when there is a tmux session to attach to, and must not double
    # up with Ghost's "Restore" button (same op, different label).
    page = web.load_page()
    assert 'else if (s.tmux_session)' in page


def test_parked_renders_as_restored_not_as_the_raw_enum():
    page = web.load_page()
    # A detached parked card still reads "restored"; the raw "parked" enum is
    # never shown. (An attached one reads "attached" — see the #32 test below.)
    assert '(s.attached ? "attached" : "restored")' in page
    assert '"parked"' in page  # still keyed on the contract value


def test_a_collapsed_worktree_row_shows_a_count_and_an_expander():
    # #34: a discoverable worktree row folds its subagent fan-out. The row
    # must show the count (dup_count) and an expander over dup_members, each
    # sibling adoptable by sid — the discoverable echo of the main page stacks.
    page = web.load_page()
    assert "if (r.dup_count > 1)" in page
    assert "r.dup_members" in page
    assert "more in this worktree" in page
    assert 'sidAction("adopt", m.session_id' in page
    assert ".dup-members" in page


def test_an_attached_parked_card_reads_attached_with_its_own_badge():
    # #32: a restored session the user has already reopened (a tmux client is
    # attached) must render "attached", get the .badge.attached colour, and
    # be explained in the legend — so a long restore list shows which are back.
    page = web.load_page()
    assert 'var parkedAttached = s.state === "parked" && s.attached;' in page
    assert ".badge.attached" in page
    assert "k-attached" in page
    assert "#key .k-attached::before" in page


def test_the_state_sort_ranks_parked():
    # STATE_ORDER has no fallback: an unranked state yields `undefined`, and
    # `undefined - 0` is NaN, which `||` treats as falsy and drops through to
    # the pid tiebreak. That makes the comparator intransitive — parked vs.
    # anything compares by pid while crashed vs. live compares by rank — and
    # Array.prototype.sort's output is then implementation-defined.
    page = web.load_page()
    assert "var STATE_ORDER = { crashed: 0, parked: 1, ghost: 2, live: 3 };" in page


def test_a_duplicate_stack_shows_the_parked_card_over_the_crashed_one():
    # stackTop shows "the one you would act on". Its `undefined ? 9` guard
    # sorts an unranked state LAST, so a stack holding a parked entry and a
    # crashed one would surface the crashed card — but the parked entry's
    # conversation is alive in tmux and is the more actionable of the two.
    page = web.load_page()
    assert "var rank = { live: 0, ghost: 1, parked: 2, crashed: 3 };" in page


def test_page_distinguishes_a_degraded_store_from_a_user_off_switch():
    page = web.load_page()
    assert "settings file unreadable" in page
    assert "global switch is off" in page


def test_page_renders_both_unknown_chips():
    # #33/#39: an unknown must be VISIBLE. Rendering nothing for it would be
    # indistinguishable from "ok" — which is exactly the fabricated
    # reassurance both issues exist to remove.
    page = web.load_page()
    assert "phone: unknown" in page
    assert "context unknown" in page
    assert "unknown-badge" in page


def test_key_explains_both_unknown_terms():
    # Every badge the card can render needs a key entry, or the user is left
    # guessing what an "unknown" chip is telling them.
    page = web.load_page()
    # Bounded by the element that FOLLOWS #key, not a magic character count:
    # the legend grew in v46 and the old 4000-char window already reached
    # past #key into the settings modal — which carries its own "Remote
    # Control" copy, so the assertion could have passed on text that is not
    # in the legend at all.
    key = page[page.index('id="key"'):page.index('id="controls"')]
    assert "phone: unknown" in key
    assert "context unknown" in key


def test_page_cards_still_headline_the_title():
    """v34 behaviour, still required: the card headline is the same string
    Claude Code shows in its mobile list."""
    assert "s.title || s.slug" in web.render_page()


def test_page_cards_headline_the_session_title():
    # The mobile list shows a title but no session id and no cwd, so the
    # title is the only string both views can share.
    page = web.render_page()
    assert "s.title || s.slug" in page   # title first, slug only as fallback
    assert "stitle" in page


def test_page_search_shows_animated_progress():
    page = web.render_page()
    assert "searching" in page
    assert "@keyframes pulse" in page
    assert "prefers-reduced-motion" in page


def test_page_has_a_settings_modal_for_exclusions():
    page = web.render_page()
    assert 'id="admin-btn"' in page and 'id="admin-modal"' in page
    assert '"/api/exclusions"' in page
    # provenance is precise and shows the full path, since with no
    # config.toml on disk these entries are BUILT-IN defaults
    assert "data.config_path" in page
    assert "built-in default" in page


def test_page_secondary_views_share_one_toolbar_row():
    page = web.render_page()
    assert 'id="tools"' in page
    # the three stacked one-button sections are gone
    assert 'id="diag">' not in page
    assert 'id="untracked">' not in page
    assert 'id="discoverable">' not in page


def test_page_empty_search_explains_itself():
    page = web.render_page()
    assert "Type a search term" in page


def test_page_untracked_uses_the_same_modal_as_discoverable():
    page = web.render_page()
    assert "MODAL_KINDS" in page
    assert 'openDisc("untracked")' in page and 'openDisc("discoverable")' in page
    assert '"/api/untracked"' in page
    # both lists paged through the same shell
    assert page.count('id="disc-modal"') == 1


def test_page_has_global_recall_search_bar():
    page = web.render_page()
    assert 'id="recall-q"' in page          # the query input
    assert "Search recent" in page          # the global button (honest label, not "all")
    assert "Search all" not in page         # must not overclaim byte-budgeted coverage
    assert "function runRecall(" in page
    assert "/api/recall?q=" in page          # GET, not sidAction
    assert "encodeURIComponent" in page      # query is URL-encoded


def test_page_cards_have_a_per_session_search_button():
    page = web.render_page()
    # a per-card Search scoped to that session's sid (GET path, not an action).
    assert "runRecall(s.session_id)" in page


def test_page_recall_reports_skipped_transcripts():
    # "no silent caps": the global sweep surfaces what the byte budget skipped.
    page = web.render_page()
    assert "skipped" in page


def test_takeover_is_a_sid_action():
    assert "takeover" in web.SID_ACTIONS


def test_page_discoverable_has_confirm_gated_takeover_button():
    page = web.render_page()
    # the button + op are present, and it's a destructive/confirm action.
    assert "Take over" in page
    assert 'op: "takeover"' in page
    assert "confirm: true" in page
    # the sid-row confirm state exists (analogue of card confirmArmed).
    assert "sidConfirmArmed" in page


def test_page_status_toast_is_color_coded_and_animated():
    page = web.render_page()
    # showNotice takes an outcome kind and drives color-coded classes.
    assert "function showNotice(text, kind, opts)" in page  # + sticky/retry (#53)
    assert "#notice.ok" in page
    assert "#notice.error" in page
    # a real entrance animation, not just display:block.
    assert "@keyframes notice-in" in page


def test_page_actions_report_both_success_and_failure():
    page = web.render_page()
    # Both the card (/api/action) and sid (/api/sid-action) handlers map the
    # outcome to a toast kind — success is no longer silent. The mapping is a
    # shared helper now that there are three outcomes, not two.
    assert page.count("noticeKind(j)") >= 2
    assert 'j.degraded ? "warn" : "ok"' in page


def test_page_retrack_rows_render_last_prompt():
    # v19: the retrack prompt div is no longer gated out — both sid-row panels
    # render last_prompt.
    page = web.render_page()
    assert 'if (op !== "retrack")' not in page


def test_page_recency_sort_keys_on_last_active_with_updated_fallback():
    # T-A: the "Recent" sort must key on last_active, falling back to
    # updated only when last_active is empty — never the reverse.
    page = web.render_page()
    assert "recencyKey" in page
    assert "s.last_active || s.updated" in page


def test_page_has_relative_time_helper():
    page = web.render_page()
    assert "function relTime(iso)" in page
    assert "just now" in page


def test_page_has_compaction_badge_and_legend_note():
    # F2: badge per card from context_pressure, plus a legend line noting
    # it's an estimate.
    page = web.render_page()
    assert "context_pressure" in page
    assert "will compact on revive" in page
    assert "estimate" in page


def test_page_renders_the_not_connected_badge():
    # spec 2026-08-09: the badge renders for "unreachable" — Claude Code's
    # own report that the phone has no live link to this session — and the
    # key legend explains the term with per-term help text (both hover title
    # AND tap->toast, the same `.kterm` mechanism every other group uses).
    page = web.load_page()
    assert "phone: not connected" in page
    assert 's.remote_control === "unreachable"' in page
    assert "remote-control-badge" in page
    assert 'kterm" data-help="' in page
    # The legend term's own text doubles as the badge label, matching the
    # existing groups (e.g. "will compact on revive") so the tap->toast
    # ("<term>: <help>") reads as a complete sentence.
    assert page.count("phone: not connected") >= 2


def test_page_shows_what_a_waiting_session_is_blocked_on():
    # `waiting_for` is Claude Code's own "blocked on the user" report (a
    # permission prompt, a question). It gets its OWN branch rather than
    # living inside the unreachable/unknown arms: a REACHABLE session
    # renders no badge at all, and a reachable session stuck on a prompt is
    # exactly the one whose owner needs telling.
    page = web.load_page()
    assert "waiting on you" in page
    assert "if (s.waiting_for) {" in page
    assert page.count("waiting on you") >= 2   # badge label + legend term


def test_page_has_no_dropped_or_off_remote_control_branches():
    # The record-counting detector's enum is gone from the contract; a card
    # branch still testing for it would silently render nothing forever.
    page = web.load_page()
    assert 's.remote_control === "dropped"' not in page
    assert 's.remote_control === "off"' not in page


def test_reachable_renders_no_badge():
    # The common case must stay silent, or every card carries a chip.
    assert 's.remote_control === "reachable"' not in web.load_page()


def test_page_settings_modal_states_autokick_keeps_the_badge():
    # Deliverable #2's required copy: turning the global switch off must
    # not read as "the badge goes away too" — the diagnosis stays.
    page = web.render_page()
    assert "keeps the badge" in page


def test_page_settings_modal_surfaces_degraded_settings_file():
    page = web.render_page()
    assert "degraded" in page.lower()


def test_page_per_session_toggle_renders_disabled_when_global_is_off():
    # Deliverable #3: never an ON it cannot honour — the button must be
    # disabled, with the reason in visible text (not just title=, which
    # never fires on a touch screen).
    page = web.render_page()
    assert '"global-off"' in page
    assert ".disabled = true" in page


def test_page_has_latest_per_cwd_marker():
    # T-B: the newest session per cwd gets a "latest" chip.
    page = web.render_page()
    assert "function latestPerCwd(sessions)" in page
    assert "latest-badge" in page


def test_page_untrack_label_present_de_tmux_label_gone():
    page = web.render_page()
    assert "Untrack" in page
    assert "De-tmux" not in page


def test_page_has_recently_untracked_section_lazy_like_diagnostics():
    # C4: a collapsible section, lazy-fetched from /api/untracked only on
    # open — never on the 5s sessions poll path (mirrors the diagnostics
    # panel pattern).
    page = web.render_page()
    assert "Recently untracked" in page
    assert '"/api/untracked"' in page
    assert "Retrack" in page
    assert '"retrack"' in page


def test_page_has_discoverable_section_lazy_with_adopt_note():
    # C4 (now a modal, v26): lazy-fetched from /api/discoverable — never on
    # the poll path — with an Adopt action and a static clarifying note
    # (adoption != a live process attachment).
    page = web.render_page()
    assert "Discoverable (untracked)" in page
    assert '"/api/discoverable"' in page   # the kind's endpoint
    assert '"?q=" + encodeURIComponent' in page  # paged/filtered fetch
    assert "Adopt" in page
    assert '"adopt"' in page
    # Discloses the competing-resume hazard: an adopted entry is always a
    # revive candidate, so if the real session is still running elsewhere
    # the watchdog will start a second `claude --resume` on it.
    assert "does NOT attach to a running process" in page
    assert "second" in page and "claude --resume" in page


def test_page_sid_action_helper_posts_json_to_sid_action_endpoint():
    # Both new sections must use the sid-keyed endpoint (not /api/action)
    # with the same JSON Content-Type CSRF gate as the existing action POST.
    page = web.render_page()
    assert '"/api/sid-action"' in page
    assert page.count('"Content-Type": "application/json"') >= 2


def test_page_renders_diagnostics_source_and_boot_provenance():
    # F12: renderDiag must show the payload's source/boot lineage (via
    # textContent — untrusted server-derived fields), not just the events.
    page = web.render_page()
    assert "d.source" in page  # source is read from the payload
    assert "d.boots" in page  # boot identity is read from the payload


def test_page_confirm_gate_state_is_module_level():
    # Review fix regression guard: the confirm-arm state must be hoisted to
    # module level (survives render()'s full card rebuild on a poll tick)
    # rather than living only in a per-button closure that a poll landing
    # mid-arm-window would silently discard.
    page = web.render_page()
    assert "var confirmArmed" in page


def test_page_scripts_pass_node_check(tmp_path):
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not available")
    page = web.render_page()
    scripts = web.extract_scripts(page)
    assert scripts, "expected at least one <script> block in the page"
    for i, js in enumerate(scripts):
        f = tmp_path / f"block{i}.js"
        f.write_text(js, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert result.returncode == 0, f"block {i} failed node --check:\n{result.stderr}"


def test_load_page_snapshots_the_template_once(monkeypatch):
    """[2026-08-01 incident] the service re-read page.html from disk per
    request, so a branch checkout under a running service served a template
    its loaded code could not substitute (raw @PLACEHOLDER@ -> JS syntax
    error -> blank dashboard). The template is a startup snapshot now."""
    web._PAGE_CACHE = None  # reset any prior snapshot
    reads = {"n": 0}
    real = web._read_page_from_disk

    def counting_read():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(web, "_read_page_from_disk", counting_read)
    first = web.load_page()
    second = web.load_page()
    assert first == second
    assert reads["n"] == 1  # second call served the snapshot, not the disk
    web._PAGE_CACHE = None  # leave no snapshot for other tests


def test_cmd_web_warms_the_page_snapshot_before_serving(monkeypatch):
    """The snapshot must be taken at service startup, not lazily at first
    request — otherwise the skew window merely shrinks instead of closing."""
    import crr.cli as cli

    warmed = {"called": False}
    monkeypatch.setattr(cli.web, "load_page", lambda: warmed.__setitem__("called", True) or "x")

    class _FakeServer:
        def __init__(self, addr, handler):
            assert warmed["called"], "load_page must be called BEFORE the server exists"

        def serve_forever(self):
            raise KeyboardInterrupt  # unwind immediately

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "ThreadingHTTPServer", _FakeServer)
    assert cli.main(["web", "--port", "1"]) == 0


def test_cmd_web_resolves_the_tab_spawner_per_action_not_once_at_startup(monkeypatch):
    """[live bug, 2026-08-09] The dashboard bound its tab spawner once, at
    service startup. On WSL that decision depends on whether the WSLInterop
    binfmt handler is registered — which can be absent at boot and repaired
    minutes later. A long-lived crr-web that started while interop was down
    held `None` for its whole life, so every Reopen click silently produced no
    tab even after the host became tab-capable. Resolve it per action.
    """
    import crr.cli as cli

    captured = {}

    def fake_make_web_handler(provider, allowed, suffixes, **kw):
        captured.update(kw)
        return object()

    class _FakeServer:
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    calls = {"n": 0}

    def counting_tab_spawner(config):
        calls["n"] += 1
        return None, True

    monkeypatch.setattr(cli, "make_web_handler", fake_make_web_handler)
    monkeypatch.setattr(cli, "ThreadingHTTPServer", _FakeServer)
    monkeypatch.setattr(cli, "_tab_spawner", counting_tab_spawner)
    assert cli.main(["web", "--port", "1"]) == 0

    # Startup must not have decided anything about tabs.
    assert calls["n"] == 0, "tab spawner resolved at startup — the stale-decision bug"

    # Each tab-capable action re-asks the host. 'remove' never spawns a tab,
    # so it must not pay for the probe either.
    action = captured["action_provider"]
    action("remove", 999999)
    assert calls["n"] == 0
    action("reopen", 999999)
    assert calls["n"] == 1
    action("reopen", 999999)
    assert calls["n"] == 2


def test_page_stack_toggle_pluralises_correctly():
    # "+2 more copyies" was the first cut.
    page = web.render_page()
    assert "copyies" not in page
    assert '" more copy" : " more copies"' in page


def test_page_search_scopes_the_card_list_and_results_are_clickable():
    """A search that leaves every non-matching card on screen isn't a
    search. Results scope the list AND jump to the card when clicked."""
    page = web.render_page()
    assert "searchMatchSids" in page          # scoping state
    assert "function jumpToSession(" in page  # click a result -> its card
    assert 'data-sid' in page                 # cards are addressable
    assert "@keyframes flash" in page         # the jumped-to card is flagged


def test_page_clearing_the_search_unscopes_the_list():
    page = web.render_page()
    assert 'addEventListener("input"' in page
    assert "searchMatchSids = null" in page


def test_page_search_does_not_blank_the_list_for_untracked_matches():
    page = web.render_page()
    assert "none of these sessions are tracked" in page


def test_page_jump_highlight_survives_the_poll_rerender():
    # The 5s poll rebuilds every card; a highlight tracked only as a DOM
    # class would vanish mid-animation and look like the click did nothing.
    page = web.render_page()
    assert "flashSid" in page and "flashUntil" in page


def test_page_untracked_search_hit_opens_the_discoverable_modal():
    """Clicking a result for a session with no card used to dead-end in a
    toast. It now opens Discoverable pre-filtered to that session, where
    Adopt / Take over live."""
    page = web.render_page()
    assert 'openDisc("discoverable", sid)' in page
    assert "function openDisc(kind, query)" in page
    # and a prefiltered-but-empty view explains itself
    assert "it may be tracked already" in page


def test_page_key_is_grouped_with_tap_and_hover_help():
    """The key was a flat run of 5 dots plus two prose paragraphs, mixing
    three different kinds of information. It is now grouped by kind, with
    the prose moved into per-term help."""
    page = web.render_page()
    for group in (">state<", ">context<", ">sid<"):
        assert group in page, group
    assert "kgroup" in page and "klabel" in page and "kterm" in page
    assert "data-help" in page
    # the old always-on prose blocks are gone
    assert "k-note" not in page


def test_page_key_help_works_on_touch_not_just_hover():
    # title= never fires on a phone, and this dashboard is used from one.
    page = web.render_page()
    assert '#key .kterm' in page
    assert 'showNotice(t.textContent + ": " + help' in page


# --- cold start: in-flight feedback + manual retry (#53) ------------------

def test_page_shows_an_in_flight_state_while_an_action_runs():
    # A cold Windows Terminal start can take ~30s. The old page showed
    # nothing at all for that whole time, so the click looked ignored.
    page = web.render_page()
    assert "noticeWorking(" in page          # a distinct in-flight notice helper
    assert "opts.sticky" in page             # ...which must not self-dismiss
    assert "noticeWorking(op)" in page       # and the card action must call it


def test_page_offers_retry_only_when_an_action_came_back_degraded():
    # Manual, never automatic: a tab spawn is not idempotent, so an
    # auto-retry after a slow-but-successful spawn opens a SECOND tab. The
    # user can look at their screen and decide; the code cannot.
    page = web.render_page()
    assert "notice-retry" in page
    assert "j.degraded" in page


def test_a_parked_card_offers_the_running_session_controls(page_source=None):
    # [#58] A parked conversation is operationally LIVE now (the journal is
    # re-keyed onto the claude in the pane), so it must offer Kick and Close
    # — the controls for "reconnect this" and "end this" — alongside the
    # re-homing pair. Reopen stays (it attaches a tab); Dismiss goes, since
    # it requires CRASHED and would only ever refuse.
    page = web.render_page()
    parked_arm = page.split('else if (s.state === "parked") {')[1].split("} else")[0]
    for label in ("Kick", "Close", "Untrack", "Un-tmux", "Reopen"):
        assert f'"{label}"' in parked_arm, f"parked card is missing {label}"
    assert '"Dismiss"' not in parked_arm


def test_the_header_counts_reachable_and_not_connected():
    """Spec Phase 3 promised this and it was silently dropped.

    With 17 cards, per-card badges alone do not answer "am I good to leave
    the desk?" without reading every card. That question is the whole point
    of the feature, so the count belongs where you can see it at a glance.
    """
    page = web.load_page()
    assert "reachableSummary" in page
    assert "not connected" in page


def test_a_not_reachable_filter_exists():
    """Also promised by the spec and dropped. After a reboot this is how you
    see only the sessions that need attention instead of scanning 17 cards."""
    page = web.load_page()
    assert 'value="unreachable"' in page
    assert 'filterKey === "unreachable"' in page


# --- the served action envelope satisfies its own validator (#55) ---------
#
# A contract nothing checks at the boundary is documentation. These are the
# tests that would have caught #49 widening the shape silently.

def test_the_served_action_result_validates_against_its_contract():
    from crr.core import contracts
    for ok, degraded in ((True, False), (True, True), (False, False)):
        resp = _post({"op": "reopen", "pid": 42},
                     action_provider=lambda o, p: (ok, "a message", degraded))
        contracts.validate_action_result(json.loads(resp.body))


def test_the_served_sid_action_result_validates_against_its_contract():
    from crr.core import contracts
    resp = _post_sid({"op": "adopt", "sid": _VALID_SID},
                     sid_action_provider=lambda o, s: (True, "adopted", False))
    contracts.validate_action_result(json.loads(resp.body))


def test_both_endpoints_serve_the_SAME_envelope():
    # One shape, not two copies free to drift — the reason #55 gives them a
    # shared constant rather than one each.
    a = json.loads(_post({"op": "reopen", "pid": 42},
                         action_provider=lambda o, p: (True, "m", False)).body)
    b = json.loads(_post_sid({"op": "adopt", "sid": _VALID_SID},
                             sid_action_provider=lambda o, s: (True, "m", False)).body)
    assert a == b


def test_a_refusal_is_stamped_too():
    # The 409 path is a served shape as much as the 200 path.
    from crr.core import contracts
    resp = _post({"op": "dismiss", "pid": 42},
                 action_provider=lambda o, p: (False, "is live", False))
    assert resp.status == 409
    contracts.validate_action_result(json.loads(resp.body))


# --- a conflicted card forces a choice (#48) ------------------------------
#
# Two live claudes on one conversation is not something to warn about and
# move on from: whichever the user ignores keeps writing to the same
# transcript, and both are reachable from the phone. So the card's ordinary
# actions are withheld until one of the two is killed.

def test_a_conflicted_card_says_two_agents_are_on_it():
    page = web.render_page()
    assert "s.conflict" in page
    assert "TWO AGENTS" in page.upper()


def test_a_conflicted_card_offers_the_kill_and_withholds_the_rest():
    page = web.render_page()
    arm = page.split("if (s.conflict)")[1].split("} else")[0]
    # The one action it does offer, and the reason the others are gone.
    assert '"close"' in arm, "no way to resolve the conflict from the card"
    assert "return" in arm, "ordinary actions must not be reachable while conflicted"


def test_the_conflict_warning_is_not_a_dismissible_toast():
    # A toast scrolls away and the conflict does not. It belongs on the card.
    page = web.render_page()
    assert "conflict-warn" in page


# --------------------------------------------------------------------------
# /qr.svg — the "Add a device" affordance. Lazy like diagnostics/discoverable
# (never on the poll path): the provider round-trips through real tailscale
# subprocess calls, so a missing provider or a "serve not live yet" None both
# degrade to a plain 404 rather than an error the <img> tag has to parse.
# --------------------------------------------------------------------------

def test_qr_svg_route_returns_svg():
    resp = _handle(path="/qr.svg",
                   qr_svg_provider=lambda: "<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/svg+xml"
    assert b"<svg" in resp.body


def test_qr_svg_route_404_without_provider():
    assert _handle(path="/qr.svg").status == 404


def test_qr_svg_route_404_when_provider_returns_none():
    # serve isn't live: the provider has nothing to offer, not an error.
    resp = _handle(path="/qr.svg", qr_svg_provider=lambda: None)
    assert resp.status == 404


def test_page_has_the_add_a_device_affordance():
    page = web.render_page()
    assert 'id="adddev-btn"' in page
    assert 'id="adddev-box"' in page
    assert 'id="adddev-qr"' in page
    assert '/qr.svg' in page


# --------------------------------------------------------------------------
# /api/machines — the launcher panel's machine list. Lazy like diagnostics/
# qr.svg (never on the poll path): the provider round-trips through real
# tailscale status calls, so a missing provider degrades to a plain 404.
# --------------------------------------------------------------------------

def test_machines_route_returns_json():
    payload = {"contract": 1, "machines": [
        {"name": "Lovelace", "url": "https://lovelace.ts.net/", "online": True, "is_self": False, "os": "linux"},
    ]}
    r = _handle("GET", "/api/machines", machines_provider=lambda: payload)
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/json"
    body = json.loads(r.body)
    assert body["contract"] == 1
    assert len(body["machines"]) == 1


def test_machines_route_no_provider_returns_404():
    r = _handle("GET", "/api/machines")
    assert r.status == 404


def test_machines_route_no_cache():
    payload = {"contract": 1, "machines": []}
    r = _handle("GET", "/api/machines", machines_provider=lambda: payload)
    assert "no-store" in r.headers.get("Cache-Control", "")


# --------------------------------------------------------------------------
# PWA assets — manifest, service worker, icons (installability).
# Backed by crr.core.pwa; these routes only wire content-type + bytes.
# --------------------------------------------------------------------------

class TestPwaRoutes:
    """Manifest, service worker, and icon routes for PWA installability."""

    def test_manifest_route(self):
        resp = _handle(path="/manifest.webmanifest")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/manifest+json"
        obj = json.loads(resp.body)
        assert obj["name"] == "Claude-Remote-Rescue"
        assert obj["display"] == "standalone"

    def test_sw_route(self):
        resp = _handle(path="/sw.js")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/javascript"
        assert b"fetch" in resp.body

    def test_icon_192_route(self):
        resp = _handle(path="/icon-192.png")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_icon_512_route(self):
        resp = _handle(path="/icon-512.png")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_apple_touch_icon_route(self):
        resp = _handle(path="/apple-touch-icon.png")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_pwa_routes_are_no_store(self):
        for path in ("/manifest.webmanifest", "/sw.js", "/icon-192.png",
                     "/icon-512.png", "/apple-touch-icon.png"):
            resp = _handle(path=path)
            assert resp.headers["Cache-Control"] == "no-store"


# --------------------------------------------------------------------------
# POST /api/reauth, POST /api/reauth-code — dashboard remote reauth (spec
# 2026-08-21). Both are non-blocking: the provider spawns/pokes the tmux
# pane and returns immediately, so these tests only assert the request is
# routed to the provider and its (ok, message, degraded) reply is stamped
# into the same envelope /api/action and /api/sid-action use.
# --------------------------------------------------------------------------

class TestPostReauth:
    def test_calls_provider_and_returns_its_result(self):
        called = []

        def reauth():
            called.append(True)
            return (True, "Reauth started — URL will appear on next poll", False)

        resp = _handle(
            "POST", "/api/reauth",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            reauth_provider=reauth,
        )
        assert resp.status == 200
        assert called
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert "started" in body["message"].lower()

    def test_failure_from_provider_is_409_not_an_exception(self):
        resp = _handle(
            "POST", "/api/reauth",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            reauth_provider=lambda: (False, "Reauth already in progress", False),
        )
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["ok"] is False

    def test_requires_json_content_type(self):
        resp = _handle(
            "POST", "/api/reauth",
            headers={"Content-Type": "text/plain"},
            body=b"{}",
            reauth_provider=lambda: (True, "url", False),
        )
        assert resp.status == 415

    def test_unavailable_when_no_provider(self):
        resp = _handle(
            "POST", "/api/reauth",
            headers={"Content-Type": "application/json"},
            body=b"{}",
        )
        assert resp.status == 503

    def test_disallowed_host_is_403_before_dispatch(self):
        resp = _handle(
            "POST", "/api/reauth", host="evil.com",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            reauth_provider=lambda: (True, "url", False),
        )
        assert resp.status == 403


class TestPostReauthCode:
    def test_calls_provider_with_code_and_returns_its_result(self):
        called = []

        def reauth_code(code):
            called.append(code)
            return (True, "Code submitted — watching for credential refresh", False)

        resp = _handle(
            "POST", "/api/reauth-code",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"code": "abc123"}).encode(),
            reauth_code_provider=reauth_code,
        )
        assert resp.status == 200
        assert called == ["abc123"]
        body = json.loads(resp.body)
        assert body["ok"] is True

    def test_requires_code_field(self):
        resp = _handle(
            "POST", "/api/reauth-code",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"wrong": "field"}).encode(),
            reauth_code_provider=lambda c: (True, "ok", False),
        )
        assert resp.status == 400

    def test_rejects_non_string_code(self):
        resp = _handle(
            "POST", "/api/reauth-code",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"code": 12345}).encode(),
            reauth_code_provider=lambda c: (True, "ok", False),
        )
        assert resp.status == 400

    def test_rejects_empty_code(self):
        resp = _handle(
            "POST", "/api/reauth-code",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"code": ""}).encode(),
            reauth_code_provider=lambda c: (True, "ok", False),
        )
        assert resp.status == 400

    def test_bad_json_is_400(self):
        resp = _handle(
            "POST", "/api/reauth-code",
            headers={"Content-Type": "application/json"},
            body=b"not json",
            reauth_code_provider=lambda c: (True, "ok", False),
        )
        assert resp.status == 400

    def test_requires_json_content_type(self):
        resp = _handle(
            "POST", "/api/reauth-code",
            headers={"Content-Type": "text/plain"},
            body=json.dumps({"code": "abc"}).encode(),
            reauth_code_provider=lambda c: (True, "ok", False),
        )
        assert resp.status == 415

    def test_unavailable_when_no_provider(self):
        resp = _handle(
            "POST", "/api/reauth-code",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"code": "abc"}).encode(),
        )
        assert resp.status == 503
