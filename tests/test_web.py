import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading

import pytest

from crr import journal, transcript, web

BOOT = "boot-test"
SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"


# ---------------------------------------------------------------------------
# Fixtures / helpers


@pytest.fixture(autouse=True)
def clear_prompt_cache():
    web._prompt_cache.clear()
    yield
    web._prompt_cache.clear()


@pytest.fixture
def server(crr_state):
    srv = web.make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, host=None, body=None, headers=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = dict(headers or {})
    if host is not None:
        hdrs["Host"] = host
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp, data
    finally:
        conn.close()


def get_json(srv, path, host=None):
    resp, data = request(srv, "GET", path, host=host)
    return resp, json.loads(data)


def post_action(srv, payload, ctype="application/json", raw=None):
    body = raw if raw is not None else json.dumps(payload)
    headers = {"Content-Type": ctype} if ctype else {}
    resp, data = request(srv, "POST", "/api/action", body=body, headers=headers)
    try:
        return resp, json.loads(data)
    except ValueError:
        return resp, None


def make_entry(pid, boot=BOOT, sid=None, verified=True, cwd="/w", **kw):
    claude = None
    if sid:
        claude = {"session_id": sid, "started": journal.now_iso(), "verified": verified}
    entry = journal.new_entry(
        pid=pid, cwd=cwd, shell="bash", host="tab", boot_id=boot, claude=claude, **kw
    )
    journal.write_entry(entry)
    return entry


def fix_boot(monkeypatch):
    from crr import bootid

    monkeypatch.setattr(bootid, "current_boot_id", lambda: BOOT)


def force_ttys(monkeypatch, mapping):
    monkeypatch.setattr(web, "_batch_tty", lambda pids: dict(mapping))


# ---------------------------------------------------------------------------
# Host-header allowlist (exact-match semantics)


def test_host_allowed_unit_tricky_cases():
    port = 8377
    ok = lambda h: web.host_allowed(h, port)
    assert ok("127.0.0.1") and ok("127.0.0.1:8377")
    assert ok("localhost") and ok("localhost:8377")
    assert ok("[::1]") and ok("[::1]:8377")
    assert ok("foo.ts.net") and ok("foo.ts.net:8377")
    hostname = socket.gethostname()
    assert ok(hostname) and ok("%s:8377" % hostname)
    # exact-match rejections
    assert not ok("evil-localhost")
    assert not ok("localhost.evil.com")
    assert not ok("evilts.net")  # no dot before ts.net
    assert not ok("localhost:9999")  # wrong port
    assert not ok("foo.ts.net:9999")
    assert not ok("")
    # config extras: exact match, with or without our port
    assert web.host_allowed("mybox.lan", port, ["mybox.lan"])
    assert web.host_allowed("mybox.lan:8377", port, ["mybox.lan"])
    assert not web.host_allowed("mybox.lan.evil.com", port, ["mybox.lan"])


def test_server_rejects_bad_hosts_with_403(server):
    for bad in ("evil-localhost", "localhost.evil.com", "evilts.net"):
        resp, _ = request(server, "GET", "/", host=bad)
        assert resp.status == 403, bad


def test_server_accepts_allowed_hosts(server):
    port = server.server_address[1]
    for good in ("localhost:%d" % port, "127.0.0.1:%d" % port, "foo.ts.net"):
        resp, _ = request(server, "GET", "/", host=good)
        assert resp.status == 200, good


def test_host_check_runs_before_routing_and_posts(server):
    # A disallowed host is 403 even on POST with a bad content type: the
    # gate fires before any routing or parsing.
    resp, _ = request(
        server, "POST", "/api/action", host="evil-localhost",
        body="a=1", headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status == 403


def test_config_extras_accepted(crr_state):
    (crr_state.parent / "config.toml").write_text(
        'host_allowlist = ["mybox.lan"]\n', encoding="utf-8"
    )
    srv = web.make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "GET", "/", host="mybox.lan")
        assert resp.status == 200
        resp, _ = request(srv, "GET", "/", host="otherbox.lan")
        assert resp.status == 403
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# POST validation (CSRF + input hygiene)


def test_post_without_json_content_type_is_415(server):
    for ctype in (None, "text/plain", "application/x-www-form-urlencoded"):
        resp, _ = post_action(server, {"action": "remove", "pid": 1}, ctype=ctype)
        assert resp.status == 415, ctype


def test_post_json_with_charset_suffix_accepted(server):
    resp, body = post_action(
        server, {"action": "remove", "pid": 424242},
        ctype="application/json; charset=utf-8",
    )
    assert resp.status == 404  # validated + dispatched (entry absent)
    assert body["status"] == "not-found"


def test_post_malformed_json_is_400(server):
    resp, _ = post_action(server, None, raw="{not json")
    assert resp.status == 400


def test_post_invalid_action_is_400(server):
    resp, _ = post_action(server, {"action": "explode", "pid": 1})
    assert resp.status == 400


def test_post_invalid_pid_is_400(server):
    for pid in ("abc", "-5", "1; rm -rf /", "12345678901", True, 3.5, None, ""):
        resp, _ = post_action(server, {"action": "remove", "pid": pid})
        assert resp.status == 400, repr(pid)


# ---------------------------------------------------------------------------
# /api/status


def test_status_shape_live_and_crashed(server, monkeypatch):
    fix_boot(monkeypatch)
    me = os.getpid()
    make_entry(me, boot=BOOT, sid=SID_A, last_cmd="vim x")
    make_entry(424242, boot="stale-boot", sid=SID_B)  # boot mismatch -> crashed
    force_ttys(monkeypatch, {me: "pts/0"})
    resp, data = get_json(server, "/api/status")
    assert resp.status == 200
    cards = {c["pid"]: c for c in data["cards"]}
    live = cards[me]
    assert live["state"] == "live"
    assert live["sid8"] == SID_A[:8]
    assert live["identity"] == "#%d · %s" % (me, SID_A[:8])
    assert live["sid_verified"] is True
    assert live["last_cmd"] == "vim x"
    assert live["cwd"] == "/w"
    assert live["shell"] == "bash"
    assert live["host"] == "tab"
    assert live["updated"]
    crashed = cards[424242]
    assert crashed["state"] == "crashed"
    assert data["counts"]["live"] == 1
    assert data["counts"]["crashed"] == 1


def test_status_ghost_when_no_tty(server, monkeypatch):
    fix_boot(monkeypatch)
    me = os.getpid()
    make_entry(me, boot=BOOT)
    force_ttys(monkeypatch, {me: "?"})
    _, data = get_json(server, "/api/status")
    assert data["cards"][0]["state"] == "ghost"


def test_status_idle_flag(server, monkeypatch):
    fix_boot(monkeypatch)
    me = os.getpid()
    entry = make_entry(me, boot=BOOT)
    entry["updated"] = "2020-01-01T00:00:00+00:00"  # long stale
    journal._atomic_write_json(journal.entry_path(me), entry)
    force_ttys(monkeypatch, {me: "pts/0"})
    _, data = get_json(server, "/api/status")
    card = data["cards"][0]
    assert card["state"] == "live"
    assert card["idle"] is True
    assert data["counts"]["idle"] == 1


def test_status_duplicate_detection(server, monkeypatch):
    fix_boot(monkeypatch)
    make_entry(424242, boot="stale", sid=SID_A)
    make_entry(424243, boot="stale", sid=SID_A)
    make_entry(424244, boot="stale", sid=SID_B)
    force_ttys(monkeypatch, {})
    _, data = get_json(server, "/api/status")
    cards = {c["pid"]: c for c in data["cards"]}
    assert cards[424242]["duplicate"] is True
    assert cards[424243]["duplicate"] is True
    assert cards[424242]["dup_group"] == cards[424243]["dup_group"] == SID_A[:8]
    assert cards[424244]["duplicate"] is False
    assert cards[424244]["dup_group"] is None
    assert data["counts"]["duplicate"] == 2


def test_status_unverified_sid_flag(server, monkeypatch):
    fix_boot(monkeypatch)
    make_entry(424242, boot="stale", sid=SID_A, verified=False)
    force_ttys(monkeypatch, {})
    _, data = get_json(server, "/api/status")
    assert data["cards"][0]["sid_verified"] is False


def test_status_last_msg_from_transcript(server, monkeypatch, tmp_path):
    fix_boot(monkeypatch)
    projects = tmp_path / "projects"
    slugdir = projects / transcript.cwd_slug("/w")
    slugdir.mkdir(parents=True)
    line = json.dumps(
        {"type": "user", "message": {"role": "user", "content": "hello from transcript"}}
    )
    (slugdir / (SID_A + ".jsonl")).write_text(line + "\n", encoding="utf-8")
    monkeypatch.setenv("CRR_CLAUDE_PROJECTS_DIR", str(projects))
    make_entry(424242, boot="stale", sid=SID_A, cwd="/w")
    force_ttys(monkeypatch, {})
    _, data = get_json(server, "/api/status")
    assert data["cards"][0]["last_msg"] == "hello from transcript"


def test_status_subprocess_budget(monkeypatch):
    """<=2 spawns per entry required; the batched implementation does at
    most ONE ps spawn for the whole request."""
    from crr import classify

    fix_boot(monkeypatch)
    for pid in range(424242, 424248):  # 6 entries
        make_entry(pid, boot=BOOT, sid=SID_A if pid % 2 else None)
    monkeypatch.setattr(classify, "pid_alive", lambda pid: True)
    real_run = subprocess.run
    calls = []

    def counting_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(web.subprocess, "run", counting_run)
    data = web.assemble_status()
    assert len(data["cards"]) == 6
    assert len(calls) <= 2
    for argv in calls:  # argv lists only, never shell strings
        assert isinstance(argv, list)


# ---------------------------------------------------------------------------
# /api/action dispatch (OpResult propagation)


def test_action_kick_on_crashed_returns_conflict(server, monkeypatch):
    fix_boot(monkeypatch)
    make_entry(424242, boot="stale-boot", sid=SID_A)
    resp, body = post_action(server, {"action": "kick", "pid": 424242})
    assert resp.status == 409  # refused, not a green checkmark
    assert body["ok"] is False
    assert body["status"] == "refused-crashed"
    assert body["state"] == "crashed"
    assert body["exit_code"] == 3


def test_action_unknown_pid_is_404_with_opresult(server):
    resp, body = post_action(server, {"action": "close", "pid": 999999})
    assert resp.status == 404
    assert body["status"] == "not-found"
    assert body["ok"] is False


def test_action_remove_success_and_string_pid(server, monkeypatch):
    fix_boot(monkeypatch)
    make_entry(424242, boot="stale")
    resp, body = post_action(server, {"action": "remove", "pid": "424242"})
    assert resp.status == 200
    assert body["ok"] is True
    assert body["status"] == "removed"
    assert journal.read_entry(424242) is None


def test_action_revive_alias_gates_like_reopen(server, monkeypatch):
    fix_boot(monkeypatch)
    me = os.getpid()
    make_entry(me, boot=BOOT, sid=SID_A)
    force_ttys(monkeypatch, {me: "pts/0"})
    monkeypatch.setattr(
        web.classify, "classify", lambda entry, boot=None: web.classify.LIVE
    )
    resp, body = post_action(server, {"action": "revive", "pid": me})
    assert resp.status == 409  # live entries are refused revival
    assert body["status"] == "refused-live"


# ---------------------------------------------------------------------------
# Page, version, self-heal headers


def test_page_serves_html(server):
    resp, data = request(server, "GET", "/")
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/html")
    text = data.decode("utf-8")
    assert "<!DOCTYPE html>" in text
    assert "PAGE_VERSION = %d" % web.PAGE_VERSION in text


def test_api_version(server):
    resp, body = get_json(server, "/api/version")
    assert resp.status == 200
    assert body == {"version": web.PAGE_VERSION}


def test_cache_control_no_store_everywhere(server):
    for path in ("/", "/api/status", "/api/version"):
        resp, _ = request(server, "GET", path)
        assert resp.getheader("Cache-Control") == "no-store", path
    resp, _ = post_action(server, {"action": "remove", "pid": 1})
    assert resp.getheader("Cache-Control") == "no-store"
    resp, _ = request(server, "GET", "/nope")
    assert resp.status == 404
    assert resp.getheader("Cache-Control") == "no-store"


def test_page_js_never_uses_innerhtml():
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", web.PAGE_HTML, re.S)
    assert scripts
    for script in scripts:
        assert "innerHTML" not in script


def test_page_scripts_pass_node_check(server, tmp_path):
    """[lesson: page self-heal] a served page is not verifiable by curl;
    every script block must be syntax-checked."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available in this environment")
    _, data = request(server, "GET", "/")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", data.decode("utf-8"), re.S)
    assert scripts, "page has no script blocks?"
    for i, script in enumerate(scripts):
        path = tmp_path / ("script_%d.js" % i)
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, "node --check failed:\n%s" % proc.stderr


# ---------------------------------------------------------------------------
# /api/diagnostics (lazy, per-source, degraded)


def test_diagnostics_shape_per_source(server, monkeypatch):
    monkeypatch.setattr(
        web, "_run_capture", lambda argv, timeout=5.0: (None, "unavailable")
    )
    resp, body = get_json(server, "/api/diagnostics")
    assert resp.status == 200
    sources = body["sources"]
    for name in ("boots", "prev_boot_errors", "host_events"):
        assert name in sources
        assert "ok" in sources[name]


def test_diagnostics_never_on_poll_path(server, monkeypatch):
    called = []
    monkeypatch.setattr(web, "diagnostics", lambda: called.append(1) or {"sources": {}})
    get_json(server, "/api/status")
    get_json(server, "/api/version")
    assert called == []


# ---------------------------------------------------------------------------
# Server binding + CLI wiring


def test_server_binds_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"


def test_cli_has_web_subcommand():
    from crr import cli

    parser = cli.build_parser()
    args = parser.parse_args(["web", "--port", "1234"])
    assert args.port == 1234
    assert args.func is cli.cmd_web


def test_make_server_uses_config_port(crr_state):
    (crr_state.parent / "config.toml").write_text("web_port = 0\n", encoding="utf-8")
    # port 0 is rejected by config validation (1..65535); default applies.
    from crr import config

    assert config.load_config()["web_port"] == 8377
