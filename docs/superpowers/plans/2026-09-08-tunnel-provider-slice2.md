# Pluggable Tunnel Support — Slice 2 (Dashboard GUI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Tunnel section in the dashboard Settings modal — provider picker, Cloudflare fields, live health/URL, explicit Up/Down actions — plus the four deferred slice-1 items (live-read allowlist, `available()` platform alignment, refusal-message prose, machines-panel CF URL).

**Architecture:** Two new endpoints (`GET/POST /api/tunnel`, `POST /api/tunnel-action`) following the existing `/api/settings` provider/writer pattern; page changes in `crr/core/page.html` (PAGE_VERSION 67 → 68); all wiring in `crr/cli.py`. Spec: `docs/superpowers/specs/2026-09-02-tunnel-provider-design.md` §Dashboard GUI (slice 2).

**Tech Stack:** Python 3.12 stdlib; vanilla JS in page.html. pytest via `.venv/bin/pytest`. Branch: `feat/tunnel-slice2` (create from main).

## Global Constraints

- One-way layering (verify `.venv/bin/lint-imports`): cli → adapters → core.
- Zero runtime dependencies. TDD: failing test first, always.
- Writing tunnel settings NEVER starts/stops a tunnel; lifecycle is only the explicit `/api/tunnel-action` (spec: "Writing settings does not start/stop anything").
- F16 tri-state: unknown health renders as unknown, never coerced.
- `SettingsStore.write_tunnel` is FULL-REPLACE (slice-1 ledger): the page always POSTs all three fields.
- Page rule: no bare numeric priors in page.html; untrusted strings rendered via textContent only.
- PAGE_VERSION bump + hash pin + changelog entry required for any page.html change (one bump for the whole branch, done in the page task).
- POST endpoints keep the house CSRF posture: JSON content-type gate, no CORS headers, ValueError → 400 with message.
- Commit per task with `--no-verify` EXCEPT the final task (hook runs the full suite once). Every commit ends with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01JKhoNtnaXXCEictTyXHk2t`
- The Bash tool's shell chokes on bash-isms; put multi-line shell (heredocs etc.) in a file via the Write tool and run `bash /path/file.sh`, or use single-line commands.

---

### Task 1: `/api/tunnel` GET + POST (contract, providers, handler)

**Files:**
- Modify: `crr/core/contracts.py` (new contract constant + validator, near `validate_settings_payload` ~line 723)
- Modify: `crr/core/web.py` (GET branch near `/api/settings` ~line 443; POST branch near the settings POST ~line 546; `make_web_handler` + `handle_request` gain `tunnel_provider_fn` and `tunnel_writer` params — read `make_web_handler`'s existing param threading and mirror `settings_provider`/`settings_writer` exactly)
- Modify: `crr/cli.py` (`tunnel_settings_provider` + `tunnel_settings_writer` closures in `_cmd_web` next to `settings_provider` ~line 5044; pass to `make_web_handler`)
- Test: `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `contracts.TUNNEL_PAYLOAD_CONTRACT_VERSION = 1`; `contracts.TUNNEL_PAYLOAD_KEYS = ("contract", "provider", "origin", "override", "config_default", "cloudflare_tunnel_name", "cloudflare_hostname", "health", "health_detail", "url", "degraded")`; `contracts.validate_tunnel_payload(payload)`.
- Produces (cli): `tunnel_settings_provider() -> dict` (the payload above; health/url from the ACTIVE provider — lazy, this endpoint is fetched only when the modal opens, like `/api/machines`); `tunnel_settings_writer(data: dict) -> dict` (validates via `SettingsStore.write_tunnel`, returns the refreshed payload).
- Payload semantics: `provider` = effective provider; `origin` = "configured"|"override"; `override` = stored override or None; `config_default` = config.toml's value; `health` ∈ ("up","down","unknown","none") — "none" when provider is none (no probe); `url` = advertise_url() or None; `degraded` = `SettingsStore.is_degraded()`. A selection `ValueError` (corrupt override) yields `provider="invalid"`, `health="unknown"`, `health_detail` naming the bad value — the GET must never 500.

- [ ] **Step 1: Write the failing tests.** In `tests/test_web.py` (mirror the existing `/api/settings` tests' fake-provider style — find them via `grep -n "api/settings" tests/test_web.py` and copy their request-construction helper):

```python
def test_tunnel_get_returns_provider_payload():
    payload = {
        "contract": contracts.TUNNEL_PAYLOAD_CONTRACT_VERSION,
        "provider": "tailscale", "origin": "configured", "override": None,
        "config_default": "tailscale", "cloudflare_tunnel_name": "",
        "cloudflare_hostname": "", "health": "up",
        "health_detail": "tailscale serve is live",
        "url": "https://x.ts.net/", "degraded": False,
    }
    contracts.validate_tunnel_payload(payload)  # the shape is contracted
    handler = web.make_web_handler(
        lambda: _sessions_payload(), {"127.0.0.1"}, (".ts.net",),
        tunnel_provider_fn=lambda: payload,
    )
    status, headers, body = _get(handler, "/api/tunnel")
    assert status == 200
    assert json.loads(body)["provider"] == "tailscale"


def test_tunnel_get_404_without_provider():
    handler = web.make_web_handler(lambda: _sessions_payload(), {"127.0.0.1"}, (".ts.net",))
    status, _, _ = _get(handler, "/api/tunnel")
    assert status == 404


def test_tunnel_post_writes_and_returns_payload():
    seen = {}

    def writer(data):
        seen.update(data)
        return {"contract": contracts.TUNNEL_PAYLOAD_CONTRACT_VERSION,
                "provider": "cloudflare", "origin": "override", "override": "cloudflare",
                "config_default": "tailscale", "cloudflare_tunnel_name": "crr",
                "cloudflare_hostname": "crr.example.com", "health": "down",
                "health_detail": "unit inactive", "url": "https://crr.example.com/",
                "degraded": False}

    handler = web.make_web_handler(
        lambda: _sessions_payload(), {"127.0.0.1"}, (".ts.net",),
        tunnel_writer=writer,
    )
    status, _, body = _post_json(handler, "/api/tunnel", {
        "provider": "cloudflare", "cloudflare_tunnel_name": "crr",
        "cloudflare_hostname": "crr.example.com"})
    assert status == 200
    assert seen["provider"] == "cloudflare"


def test_tunnel_post_rejects_bad_shape_and_bad_value():
    handler = web.make_web_handler(
        lambda: _sessions_payload(), {"127.0.0.1"}, (".ts.net",),
        tunnel_writer=lambda d: (_ for _ in ()).throw(ValueError("bad provider")),
    )
    status, _, _ = _post_json(handler, "/api/tunnel", {"nope": 1})
    assert status == 400  # missing keys rejected before the writer runs
    status, _, body = _post_json(handler, "/api/tunnel", {
        "provider": "ngrok", "cloudflare_tunnel_name": None, "cloudflare_hostname": None})
    assert status == 400 and b"bad provider" in body
```

If `tests/test_web.py` has no `_post_json`/`_get` helpers, find how the exclusions/settings POST tests build requests and use that exact mechanism (do not invent a new helper if one exists).

In `tests/test_cli.py` (provider wiring — reuse `_FakeTunnelProvider` and the settings-store write pattern already in the file):

```python
def test_tunnel_settings_provider_payload_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider(name="tailscale", health_state="up",
                               url="https://x.ts.net/")
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    payload = cli._tunnel_payload(cli.cfg.Config(), tmp_path)
    cli.contracts.validate_tunnel_payload(payload)
    assert payload["provider"] == "tailscale"
    assert payload["health"] == "up"
    assert payload["url"] == "https://x.ts.net/"


def test_tunnel_payload_never_raises_on_corrupt_override(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _write_bad_tunnel_provider(tmp_path)  # helper exists since slice 1
    payload = cli._tunnel_payload(cli.cfg.Config(), tmp_path)
    cli.contracts.validate_tunnel_payload(payload)
    assert payload["provider"] == "invalid"
    assert payload["health"] == "unknown"
    assert "ngrok" in payload["health_detail"]


def test_tunnel_payload_provider_none_health_none(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    from crr.core import settings as settings_mod
    settings_mod.SettingsStore(tmp_path).write_tunnel(provider="none")
    payload = cli._tunnel_payload(cli.cfg.Config(), tmp_path)
    assert payload["provider"] == "none" and payload["health"] == "none"
```

- [ ] **Step 2: Run, verify RED** — `.venv/bin/pytest tests/test_web.py tests/test_cli.py -q -k "tunnel_get or tunnel_post or tunnel_settings or tunnel_payload"` — expect TypeError (unknown kwargs) / AttributeError (`_tunnel_payload`).

- [ ] **Step 3: Implement.**

`crr/core/contracts.py` (constants near SETTINGS_CONTRACT_VERSION; validator near validate_settings_payload):

```python
TUNNEL_PAYLOAD_CONTRACT_VERSION = 1
TUNNEL_PAYLOAD_KEYS = (
    "contract", "provider", "origin", "override", "config_default",
    "cloudflare_tunnel_name", "cloudflare_hostname", "health",
    "health_detail", "url", "degraded",
)
TUNNEL_HEALTH_STATES = ("up", "down", "unknown", "none")


def validate_tunnel_payload(payload: Any) -> None:
    """`override` and `url` are nullable; `provider` may be "invalid" when a
    hand-edited override names an unknown provider — the GET must render the
    problem, never 500 on it (same never-500 posture as /api/settings)."""
    payload = _require_mapping(payload, "/api/tunnel payload")
    _require_exact_keys(payload, TUNNEL_PAYLOAD_KEYS, "/api/tunnel payload")
    _require_contract(payload, TUNNEL_PAYLOAD_CONTRACT_VERSION, "/api/tunnel")
    for field in ("provider", "origin", "config_default",
                  "cloudflare_tunnel_name", "cloudflare_hostname",
                  "health", "health_detail"):
        _require_type(payload[field], str, f"/api/tunnel '{field}'")
    _require_enum(payload["health"], TUNNEL_HEALTH_STATES, "/api/tunnel 'health'")
    _require_type(payload["degraded"], bool, "/api/tunnel 'degraded'")
    if payload["override"] is not None:
        _require_type(payload["override"], str, "/api/tunnel 'override'")
    if payload["url"] is not None:
        _require_type(payload["url"], str, "/api/tunnel 'url'")
```

`crr/core/web.py`: add `tunnel_provider_fn: Callable[[], dict] | None = None` and `tunnel_writer: Callable[[dict], dict] | None = None` to BOTH `make_web_handler` and `handle_request` (thread exactly as `settings_provider`/`settings_writer` are threaded — read those paths first). GET branch (beside `/api/settings` GET):

```python
        if path == "/api/tunnel":
            # Lazy like /api/machines: real subprocess probes (unit state,
            # serve status) — fetched only when the Settings modal opens,
            # never on the poll path.
            if tunnel_provider_fn is None:
                return _plain(404, "not found")
            return _json(200, tunnel_provider_fn())
```

POST branch (beside the settings POST, same gates):

```python
        if path == "/api/tunnel":
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            required = ("provider", "cloudflare_tunnel_name", "cloudflare_hostname")
            if not isinstance(data, dict) or set(data) != set(required):
                return _plain(400, 'expected {"provider", "cloudflare_tunnel_name", '
                                   '"cloudflare_hostname"} (each string or null)')
            if tunnel_writer is None:
                return _plain(503, "tunnel settings unavailable")
            try:
                return _json(200, tunnel_writer(data))
            except ValueError as exc:
                return _plain(400, str(exc))
```

`crr/cli.py` — module-level payload builder plus closures in `_cmd_web`:

```python
def _tunnel_payload(config: cfg.Config, sd) -> dict:
    """The /api/tunnel GET payload. Never raises: a corrupt override renders
    provider="invalid" with the ValueError text as health_detail (the modal
    must show the problem; a 500 would show nothing)."""
    store = settings.SettingsStore(sd)
    override = store.read_tunnel()
    try:
        sel = _tunnel_selection(config, sd)
    except ValueError as exc:
        return {
            "contract": contracts.TUNNEL_PAYLOAD_CONTRACT_VERSION,
            "provider": "invalid", "origin": "override",
            "override": override.get("provider"),
            "config_default": config.get("tunnel_provider"),
            "cloudflare_tunnel_name": override.get("cloudflare_tunnel_name")
                or config.get("cloudflare_tunnel_name"),
            "cloudflare_hostname": override.get("cloudflare_hostname")
                or config.get("cloudflare_hostname"),
            "health": "unknown", "health_detail": str(exc),
            "url": None, "degraded": store.is_degraded(),
        }
    provider = _tunnel_provider(config, sel)
    if provider is None:
        health, detail, url = "none", "no tunnel provider selected", None
    else:
        h = provider.health()
        health, detail, url = h.state, h.detail, provider.advertise_url()
    return {
        "contract": contracts.TUNNEL_PAYLOAD_CONTRACT_VERSION,
        "provider": sel.provider, "origin": sel.origin,
        "override": override.get("provider"),
        "config_default": config.get("tunnel_provider"),
        "cloudflare_tunnel_name": sel.tunnel_name,
        "cloudflare_hostname": sel.hostname,
        "health": health, "health_detail": detail,
        "url": url, "degraded": store.is_degraded(),
    }
```

In `_cmd_web`, beside `settings_provider`:

```python
    def tunnel_settings_provider() -> dict:
        payload = _tunnel_payload(config, sd)
        contracts.validate_tunnel_payload(payload)  # validate our own output
        return payload

    def tunnel_settings_writer(data: dict) -> dict:
        # SettingsStore.write_tunnel raises SettingsError (a ValueError) on a
        # bad provider string -> 400 with message, same as settings_writer.
        # Full-replace by design (slice-1 ledger): the page sends all three.
        with mutation_lock(sd):
            settings.SettingsStore(sd).write_tunnel(
                provider=data["provider"] or None,
                cloudflare_tunnel_name=data["cloudflare_tunnel_name"] or None,
                cloudflare_hostname=data["cloudflare_hostname"] or None,
            )
        return tunnel_settings_provider()
```

Pass both to `make_web_handler(...)` beside settings_provider/settings_writer. NOTE: `_cmd_web`'s state-dir local may not be named `sd` — read the surrounding code and use the actual name. Check `SettingsStore.is_degraded()` exists (it does — slice-1 settings work); if its exact name differs, use the real one.

- [ ] **Step 4: Run, verify GREEN** — same -k filter, then `.venv/bin/pytest tests/test_web.py tests/test_cli.py tests/test_contracts.py -q`.

- [ ] **Step 5: Commit** — `git add -A && git commit --no-verify -m "feat(tunnel): /api/tunnel GET/POST — contracted payload + settings writer"` (with the two trailer lines).

---

### Task 2: `POST /api/tunnel-action` (explicit up/down)

**Files:**
- Modify: `crr/core/web.py` (POST branch beside `/api/tunnel`; `tunnel_action_provider: Callable[[str], dict] | None = None` threaded through both functions)
- Modify: `crr/cli.py` (closure in `_cmd_web`)
- Test: `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `_tunnel_selection`, `_tunnel_provider`, `_tunnel_payload` (Task 1).
- Produces: `tunnel_action_provider(action: str) -> dict` returning `{"ok": bool, "message": str, "tunnel": <GET payload>}`. Actions: `"up"` | `"down"` only; anything else → the web layer 400s before the provider runs.

- [ ] **Step 1: Failing tests.** `tests/test_web.py`:

```python
def test_tunnel_action_up_dispatches_and_returns_result():
    calls = []

    def actor(action):
        calls.append(action)
        return {"ok": True, "message": "started", "tunnel": {"provider": "cloudflare"}}

    handler = web.make_web_handler(
        lambda: _sessions_payload(), {"127.0.0.1"}, (".ts.net",),
        tunnel_action_provider=actor,
    )
    status, _, body = _post_json(handler, "/api/tunnel-action", {"action": "up"})
    assert status == 200 and calls == ["up"]


def test_tunnel_action_rejects_unknown_action_before_provider():
    handler = web.make_web_handler(
        lambda: _sessions_payload(), {"127.0.0.1"}, (".ts.net",),
        tunnel_action_provider=lambda a: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    status, _, _ = _post_json(handler, "/api/tunnel-action", {"action": "restart"})
    assert status == 400


def test_tunnel_action_failure_is_200_with_ok_false():
    # A refused start (missing prereqs) is a RESULT, not a transport error:
    # the modal shows the message; only transport/shape problems are 4xx.
    handler = web.make_web_handler(
        lambda: _sessions_payload(), {"127.0.0.1"}, (".ts.net",),
        tunnel_action_provider=lambda a: {"ok": False, "message": "missing prereqs",
                                          "tunnel": {}},
    )
    status, _, body = _post_json(handler, "/api/tunnel-action", {"action": "up"})
    assert status == 200 and json.loads(body)["ok"] is False
```

`tests/test_cli.py`:

```python
def test_tunnel_action_provider_up_down(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider()
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    result = cli._tunnel_action(cli.cfg.Config(), tmp_path, "up")
    assert result["ok"] is True
    assert fake.started_with == cli.cfg.DEFAULTS["dashboard_port"]
    assert "tunnel" in result
    result = cli._tunnel_action(cli.cfg.Config(), tmp_path, "down")
    assert result["ok"] is True and fake.stopped


def test_tunnel_action_provider_none_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: None)
    result = cli._tunnel_action(cli.cfg.Config(), tmp_path, "up")
    assert result["ok"] is False and "none" in result["message"]
```

- [ ] **Step 2: RED** — expect TypeError / AttributeError.

- [ ] **Step 3: Implement.** web.py POST branch (same gates as /api/tunnel POST):

```python
        if path == "/api/tunnel-action":
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            if not isinstance(data, dict) or data.get("action") not in ("up", "down"):
                return _plain(400, 'expected {"action": "up"|"down"}')
            if tunnel_action_provider is None:
                return _plain(503, "tunnel actions unavailable")
            return _json(200, tunnel_action_provider(data["action"]))
```

cli.py module-level:

```python
def _tunnel_action(config: cfg.Config, sd, action: str) -> dict:
    """Dashboard tunnel lifecycle. A refused start/stop is ok=False with the
    provider's message — a result the modal renders, never a 4xx/500."""
    try:
        sel = _tunnel_selection(config, sd)
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "tunnel": _tunnel_payload(config, sd)}
    provider = _tunnel_provider(config, sel)
    if provider is None:
        return {"ok": False, "message": "tunnel provider is none — pick one first",
                "tunnel": _tunnel_payload(config, sd)}
    if action == "up":
        ok, msg = provider.start(config.get("dashboard_port"))
    else:
        ok, msg = provider.stop()
    return {"ok": ok, "message": msg, "tunnel": _tunnel_payload(config, sd)}
```

`_cmd_web` closure: `def tunnel_action_provider(action): return _tunnel_action(config, sd, action)` — threaded to `make_web_handler`.

- [ ] **Step 4: GREEN** — `.venv/bin/pytest tests/test_web.py tests/test_cli.py -q`.
- [ ] **Step 5: Commit** `feat(tunnel): /api/tunnel-action — explicit up/down from the dashboard` (trailers).

---

### Task 3: Settings-modal Tunnel section (page v68)

**Files:**
- Modify: `crr/core/page.html` — markup between the Auto-kick section (`<h3 …>Auto-kick dropped Remote Control sessions</h3>` block ending before line ~461) and the `Excluded directories` h3; JS beside `renderAutokick`/`loadAutokick` (~line 2054); modal-open handler (~line 2104) gains `loadTunnel()`.
- Modify: `crr/core/web.py` PAGE_VERSION 67 → 68 (comment: `# v68: Settings-modal Tunnel section (provider picker, CF fields, health, Up/Down)`).
- Modify: `tests/test_web.py` (`test_page_version_is_67` → `_68` + changelog line), `tests/test_page_version_guard.py` (append 68 pin, computed AFTER all page edits).
- Test: `tests/test_web.py`.

**Interfaces:**
- Consumes: `/api/tunnel` GET/POST and `/api/tunnel-action` (Tasks 1–2), exact payload keys from Task 1.

- [ ] **Step 1: Failing tests** (append to tests/test_web.py; rename the version test and add its changelog line the way v67's entry did):

```python
def test_page_has_a_tunnel_settings_section():
    page = web.load_page()
    assert 'id="tunnel-provider"' in page       # the picker
    assert 'id="tunnel-cf-name"' in page
    assert 'id="tunnel-cf-hostname"' in page
    assert 'id="tunnel-health"' in page
    assert 'id="tunnel-up"' in page and 'id="tunnel-down"' in page
    assert "/api/tunnel-action" in page
    # Saving settings never starts/stops a tunnel (spec) — the save handler
    # must not touch the action endpoint; pin by distinct function names.
    assert "function saveTunnel(" in page and "function tunnelAction(" in page
```

- [ ] **Step 2: RED.**

- [ ] **Step 3: Implement.** Markup (insert AFTER the autokick section's closing `</div>`, BEFORE the `Excluded directories` h3 — match the surrounding inline-style conventions exactly):

```html
    <div style="padding: 0 16px 14px;">
      <h3 style="margin:0 0 6px; font-size:12px; color:#8a93a2; font-weight:600;">Tunnel</h3>
      <label style="display:block; margin-bottom:6px;">Provider
        <select id="tunnel-provider">
          <option value="">default (config.toml)</option>
          <option value="tailscale">tailscale</option>
          <option value="cloudflare">cloudflare</option>
          <option value="none">none</option>
        </select>
      </label>
      <label style="display:block; margin-bottom:6px;">Cloudflare tunnel name
        <input type="text" id="tunnel-cf-name" placeholder="crr">
      </label>
      <label style="display:block; margin-bottom:6px;">Cloudflare hostname
        <input type="text" id="tunnel-cf-hostname" placeholder="crr.example.com">
      </label>
      <div id="tunnel-health" style="font-size:12px; color:#8a93a2; margin:6px 0;"></div>
      <div id="tunnel-degraded" hidden style="font-size:12px; color:#e0a53b; margin:6px 0;"></div>
      <button id="tunnel-save">Save</button>
      <button id="tunnel-up">Up</button>
      <button id="tunnel-down">Down</button>
    </div>
```

JS (beside `renderAutokick`; every server string via textContent — never innerHTML):

```javascript
// Tunnel section (spec 2026-09-02, slice 2). Save writes overrides ONLY —
// it never starts/stops anything; Up/Down are the explicit lifecycle
// actions (spec: "Writing settings does not start/stop anything").
function renderTunnel(data) {
  document.getElementById("tunnel-provider").value = data.override || "";
  document.getElementById("tunnel-cf-name").value = data.cloudflare_tunnel_name || "";
  document.getElementById("tunnel-cf-hostname").value = data.cloudflare_hostname || "";
  var h = document.getElementById("tunnel-health");
  // F16: "unknown" renders as unknown, never coerced into up/down.
  h.textContent = "provider: " + data.provider + " (" + data.origin + ") · health: "
    + data.health + " — " + data.health_detail + (data.url ? " · " + data.url : "");
  var warn = document.getElementById("tunnel-degraded");
  warn.hidden = !data.degraded;
  warn.textContent = data.degraded
    ? "The settings file is unreadable — overrides are ignored (config.toml rules) until this is fixed."
    : "";
}

function loadTunnel() {
  fetch("/api/tunnel")
    .then(function (r) { return r.json(); })
    .then(renderTunnel)
    .catch(function () { showNotice("tunnel settings unavailable", "warn"); });
}

function saveTunnel() {
  var btn = document.getElementById("tunnel-save");
  btn.disabled = true;
  fetch("/api/tunnel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: document.getElementById("tunnel-provider").value || null,
      cloudflare_tunnel_name: document.getElementById("tunnel-cf-name").value || null,
      cloudflare_hostname: document.getElementById("tunnel-cf-hostname").value || null
    })
  })
    .then(function (r) {
      if (!r.ok) { return r.text().then(function (t) { throw new Error(t); }); }
      return r.json();
    })
    .then(function (data) { renderTunnel(data); showNotice("tunnel settings saved", "ok"); })
    .catch(function (e) { showNotice(String(e.message || "save failed"), "error"); loadTunnel(); })
    .finally(function () { btn.disabled = false; });
}

function tunnelAction(action) {
  var btn = document.getElementById("tunnel-" + action);
  btn.disabled = true;
  fetch("/api/tunnel-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: action })
  })
    .then(function (r) {
      if (!r.ok) { return r.text().then(function (t) { throw new Error(t); }); }
      return r.json();
    })
    .then(function (data) {
      showNotice(data.message, data.ok ? "ok" : "error");
      if (data.tunnel && data.tunnel.provider) { renderTunnel(data.tunnel); }
    })
    .catch(function (e) { showNotice(String(e.message || "action failed"), "error"); })
    .finally(function () { btn.disabled = false; });
}

document.getElementById("tunnel-save").addEventListener("click", saveTunnel);
document.getElementById("tunnel-up").addEventListener("click", function () { tunnelAction("up"); });
document.getElementById("tunnel-down").addEventListener("click", function () { tunnelAction("down"); });
```

Add `loadTunnel();` inside the existing `admin-btn` click handler beside `loadAutokick();`. Bump PAGE_VERSION to 68 with its comment. Rename the version test with a v68 changelog line. LAST: recompute the page sha256 (`python3 -c "import hashlib;print(hashlib.sha256(open('crr/core/page.html','rb').read()).hexdigest())"`) and append `68: "<sha>"` to PAGE_PINS.

- [ ] **Step 4: GREEN** — `.venv/bin/pytest tests/test_web.py tests/test_page_version_guard.py -q` (the guard also runs a `node --check` over page scripts if node exists — a JS syntax error fails here).
- [ ] **Step 5: Commit** `feat(dashboard): Tunnel section in Settings — picker, CF fields, health, explicit Up/Down (v68)` (trailers).

---

### Task 4: Live-read host allowlist

**Files:**
- Modify: `crr/core/web.py` (`make_web_handler` accepts `allowed_hosts: set[str] | Callable[[], set[str]]`; resolve per request — read the function first: the allowlist check happens at the top of `handle_request`)
- Modify: `crr/cli.py` (pass `lambda: _web_allowed_hosts(config, sd)` instead of the snapshot)
- Test: `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `_web_allowed_hosts(config, sd)` (slice 1).
- Produces: callable-allowlist support; existing set-passing callers unchanged.

- [ ] **Step 1: Failing test** (tests/test_web.py):

```python
def test_allowed_hosts_callable_is_resolved_per_request():
    hosts = {"127.0.0.1"}
    handler = web.make_web_handler(lambda: _sessions_payload(), lambda: set(hosts), (".ts.net",))
    status, _, _ = _get(handler, "/api/version", host="crr.example.com")
    assert status in (403, 421)  # whatever the existing rejection status is — match it
    hosts.add("crr.example.com")
    status, _, _ = _get(handler, "/api/version", host="crr.example.com")
    assert status == 200
```

Read the existing host-rejection tests first for the exact rejection status and the `_get(..., host=)` mechanism; mirror them. In tests/test_cli.py:

```python
def test_cmd_web_passes_a_live_allowlist_callable(tmp_path, monkeypatch):
    # A GUI hostname change must take effect without a service restart
    # (slice-1 ledger item) — the handler must get a CALLABLE, not a snapshot.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    seen = {}

    def fake_make(provider, allowed, suffixes, **kw):
        seen["allowed"] = allowed
        raise SystemExit(0)  # stop before binding a socket

    monkeypatch.setattr(cli.web, "make_web_handler", fake_make)
    with pytest.raises(SystemExit):
        cli.main(["web"])
    assert callable(seen["allowed"])
```

(If `cli.main(["web"])` does heavy setup before make_web_handler, read `_cmd_web` and monkeypatch whatever earlier heavy pieces need stubbing — keep the assertion the same.)

- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement.** In `make_web_handler`/`handle_request`, where the allowlist is consulted: `hosts = allowed_hosts() if callable(allowed_hosts) else allowed_hosts` immediately before the host check, per request. cli passes the lambda. Docstring line: "a callable is re-resolved per request, the auth_enabled_fn pattern — GUI-written hostnames take effect live."
- [ ] **Step 4: GREEN** — `.venv/bin/pytest tests/test_web.py tests/test_cli.py -q`.
- [ ] **Step 5: Commit** `feat(tunnel): live-read the host allowlist — GUI hostname changes need no restart` (trailers).

---

### Task 5: Deferred slice-1 alignments (available(), refusal prose, machines CF URL)

**Files:**
- Modify: `crr/adapters/cloudflared.py` (`available()`, the missing-fields refusal message)
- Modify: `crr/cli.py` (`machines_provider` in `_cmd_web`)
- Test: `tests/test_cloudflared.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `RealCloudflared`, `_tunnel_selection`, `_tunnel_provider`, `tailnet.plan_launcher`/`MachineRow`.

- [ ] **Step 1: Failing tests.** tests/test_cloudflared.py:

```python
def test_available_requires_systemctl_too(monkeypatch):
    # Spec: on hosts without systemd --user the adapter reports the gap —
    # available() must agree with start()/stop()'s gate, not contradict it.
    monkeypatch.setattr(cloudflared.shutil, "which",
                        lambda b: "/usr/bin/cloudflared" if b == "cloudflared" else None)
    assert cloudflared.RealCloudflared(2.0, "crr", "crr.example.com").available() is False
    monkeypatch.setattr(cloudflared.shutil, "which", lambda b: "/usr/bin/" + b)
    assert cloudflared.RealCloudflared(2.0, "crr", "crr.example.com").available() is True


def test_refusal_message_names_the_real_surfaces(monkeypatch):
    monkeypatch.setattr(cloudflared.shutil, "which", lambda b: "/usr/bin/" + b)
    cf = cloudflared.RealCloudflared(2.0, "", "")
    ok, msg = cf.start(8377)
    assert not ok
    assert "dashboard Settings" in msg  # the GUI exists now (slice 2)
    assert "config.toml" in msg
```

tests/test_cli.py:

```python
def test_machines_provider_cloudflare_shows_self_url(tmp_path, monkeypatch):
    # Spec: "with provider cloudflare it shows only this machine's URL" —
    # Cloudflare has no peer concept, so the launcher lists just this host.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    from crr.core import settings as settings_mod
    settings_mod.SettingsStore(tmp_path).write_tunnel(
        provider="cloudflare", cloudflare_tunnel_name="crr",
        cloudflare_hostname="crr.example.com")
    fake = _FakeTunnelProvider(name="cloudflare", url="https://crr.example.com/")
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    payload = cli._machines_payload(cli.cfg.Config(), tmp_path)
    cli.contracts.validate_machines_payload(payload)
    (row,) = payload["machines"]
    assert row["url"] == "https://crr.example.com/"
    assert row["is_self"] is True
```

- [ ] **Step 2: RED** (the machines test fails on `_machines_payload` not existing — extracting the closure body into a module-level `_machines_payload(config, sd)` the closure delegates to is part of this task; read the existing `machines_provider` closure at cli.py ~4600 first and preserve its tailnet path byte-for-byte).

- [ ] **Step 3: Implement.**
  - `available()`: `return shutil.which("cloudflared") is not None and shutil.which("systemctl") is not None`, with a comment tying it to the start/stop gate.
  - Refusal message: `"cloudflare_tunnel_name and cloudflare_hostname must be set (config.toml, or the dashboard Settings → Tunnel section)"`.
  - `_machines_payload(config, sd)`: resolve selection (ValueError → fall through to tailnet path); if effective provider == "cloudflare", build `[tailnet.MachineRow(name=socket.gethostname().lower(), url=provider.advertise_url() or "", online=True, is_self=True, os="")._asdict()]` payload (contract-stamped + validated like the existing closure); else run the existing tailnet body unchanged. The closure becomes `return _machines_payload(config, sd)`.
  - Check `validate_machines_payload` row requirements (crr/core/contracts.py) before finalizing row fields — if `os`/`online` have enum/type constraints, satisfy them with honest values (`online=True` is honest: this machine is serving the page).
- [ ] **Step 4: GREEN** — `.venv/bin/pytest tests/test_cloudflared.py tests/test_cli.py tests/test_web.py -q`.
- [ ] **Step 5: Commit** `feat(tunnel): available() honors the systemd gate; honest refusal prose; machines panel shows the CF self-URL` (trailers).

---

### Task 6: Docs, todo, gates

**Files:**
- Modify: `docs/tunnels.md`, `todo.md`

- [ ] **Step 1:** `docs/tunnels.md`: replace the "A dashboard Settings override is planned (slice 2); today config.toml is the configuration surface." sentence with: "Pick it in `config.toml` or the dashboard's Settings → Tunnel section (the override wins; clearing it falls back to config.toml). Save only stores settings — Up/Down in the same section are the explicit lifecycle actions." Update setup step 4 to mention both surfaces again.
- [ ] **Step 2:** `todo.md`: the tunnel item's checkbox becomes `[x]`; replace its sub-line with `      Slice 1 (core + CLI) merged as PR #124; slice 2 (dashboard GUI) merged as <this PR>. macOS/Windows cloudflared lifecycle remains future work.`
- [ ] **Step 3: Full gates.** `.venv/bin/pytest -q` (expect all pass + platform skips) and `.venv/bin/lint-imports` (contract kept).
- [ ] **Step 4: Final commit WITHOUT --no-verify** — `docs(tunnel): GUI configuration surface documented; todo closed out` (trailers). The hook runs the full suite once for the branch.
- [ ] **Step 5:** Push and open the PR (title `feat: tunnel slice 2 — dashboard Tunnel settings (picker, health, explicit Up/Down)`); do NOT merge — the controller handles merge after final review.
