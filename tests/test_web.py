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
    return {"contract": 1, "sessions": []}


def _handle(method="GET", path="/", host="localhost", provider=None,
            body=b"", headers=None, action_provider=None):
    h = {"Host": host}
    if headers:
        h.update(headers)
    return web.handle_request(
        method, path, h, body,
        sessions_provider=provider or _payload,
        action_provider=action_provider,
        allowed_hosts=ALLOWED,
        allowed_suffixes=SUFFIXES,
    )


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
        return True, f"reopened {pid}"
    resp = _post({"op": "reopen", "pid": 42}, action_provider=act)
    assert resp.status == 200
    assert seen["call"] == ("reopen", 42)
    assert json.loads(resp.body) == {"ok": True, "message": "reopened 42"}


def test_post_action_gate_refusal_is_409():
    resp = _post({"op": "dismiss", "pid": 42}, action_provider=lambda o, p: (False, "is live"))
    assert resp.status == 409
    assert json.loads(resp.body)["ok"] is False


def test_post_without_json_content_type_is_415():
    resp = _post({"op": "remove", "pid": 42}, headers={}, action_provider=lambda o, p: (True, "ok"))
    assert resp.status == 415


def test_post_bad_json_is_400():
    resp = _post(raw=b"{not json", action_provider=lambda o, p: (True, "ok"))
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
    resp = _post(payload, action_provider=lambda o, p: (True, "should-not-run"))
    assert resp.status == 400


def test_post_disallowed_host_is_403_before_dispatch():
    called = []
    resp = _post({"op": "remove", "pid": 42}, host="evil.com",
                 action_provider=lambda o, p: called.append(1) or (True, "ok"))
    assert resp.status == 403
    assert called == []  # never dispatched


def test_post_to_non_action_path_is_404():
    resp = _handle(method="POST", path="/api/other", body=b"{}", headers=_JSON)
    assert resp.status == 404


def test_options_preflight_is_rejected():
    # The CSRF defense relies on the forced preflight failing. There is no
    # do_OPTIONS handler; guard that a future one can't silently reopen it.
    assert _handle(method="OPTIONS", path="/api/action").status == 405


# --------------------------------------------------------------------------
# node --check gate: every <script> in the served page must parse.
# --------------------------------------------------------------------------

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
