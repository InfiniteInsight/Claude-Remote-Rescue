# Launcher — Machines Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lazy "Machines" panel to the crr dashboard that lists every `tag:crr` node on the user's tailnet, linking each to its own dashboard, with an on-tailnet / offline badge.

**Architecture:** `plan_launcher()` is a pure core function (no I/O) that takes a parsed `tailscale status --json` dict and returns sorted `MachineRow` named tuples. The `/api/machines` route is lazy (fetched when the panel opens, like `/api/diagnostics`), wired through a `machines_provider` closure in `cli.py`. The page.html panel renders rows using `textContent` for untrusted fields and `href` for links.

**Tech Stack:** Python stdlib only (zero runtime deps). Existing test infrastructure (pytest).

## Global Constraints

- **Zero runtime dependencies.** Web server stays stdlib-`http` only.
- **One-way layering** (`.importlinter`-enforced): `cli → adapters → core`. `crr.core` never imports adapters or cli.
- **`textContent` for untrusted fields**, `href` for links. No new CORS headers. No new POST routes.
- **`PAGE_VERSION`** bumped on every `page.html` change (enforced by `test_page_version_guard.py`).
- **Contract/config version constants** bumped with their payloads, with ledger comments (enforced by `test_version_ledger.py`).
- **Badge text: "on tailnet" / "offline"**, never "live" (the badge reflects tailnet reachability, not dashboard-up).
- **URL from `DNSName`** (metacharacter-free), **name from `HostName`** (display only).
- **Self always included** in the machine list (even if Self is untagged). The "no tag:crr machines found" note shows when there are no tagged *peers*, not when Self is the only row.
- **Degrade, never crash:** `status is None` → empty list; no tagged nodes → renders "no tag:crr machines found — see setup" note (Self row is still shown if Self is in the status).
- **No test** runs real tailscale, hits the network, or opens a real browser.

## Design decision: Self-inclusion

The spec says "select Self + every Peer whose Tags contains tag." This plan reads that as **Self always included** regardless of whether Self is tagged. The "no tag:crr machines found" note (spec line 87) renders when there are no tagged peers — Self alone doesn't suppress it. Three test states cover this:

1. `status is None` → `[]` (empty, note shown)
2. Self present, no tagged peers → `[Self]` (Self row shown, note shown)
3. Self + tagged peers → sorted list (note hidden)

## Design decision: File placement

The spec line 73 says `crr/core/launcher.py`, but `crr/core/tailnet.py:4` already declares "Phase 3 (launcher) adds `plan_launcher()` here alongside `self_dashboard_url()`." This plan puts `plan_launcher()` in **`tailnet.py`** — it needs `_dashboard_url()` which is already there, and a separate `launcher.py` would either duplicate that helper or import it. The docstring at `tailnet.py:4` will be updated to reflect that `plan_launcher` has landed.

---

### Task 1: Pure `plan_launcher()` in `crr/core/tailnet.py`

**Files:**
- Modify: `crr/core/tailnet.py`
- Test: `tests/test_tailnet.py`

**Interfaces:**
- Consumes: `_dashboard_url(dnsname)` (already in `tailnet.py`)
- Produces:
  - `MachineRow` — a `typing.NamedTuple` with fields `name: str`, `url: str`, `online: bool`, `is_self: bool`
  - `plan_launcher(status: dict | None, *, tag: str, self_dnsname: str | None) -> list[MachineRow]`

- [ ] **Step 1: Write the failing tests**

Add tests to `tests/test_tailnet.py`. The fixture must reflect the real `tailscale status --json` shape: peers live under `"Peer"` as a **dict keyed by node key** (not a list), and `"Tags"` is **omitted** (not an empty list) on untagged nodes.

```python
from crr.core.tailnet import MachineRow, plan_launcher

# -- Fixtures --

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tailnet.py -v -k "plan_launcher"`
Expected: FAIL — `ImportError: cannot import name 'MachineRow' from 'crr.core.tailnet'`

- [ ] **Step 3: Implement `MachineRow` and `plan_launcher`**

In `crr/core/tailnet.py`, add after the existing `self_dashboard_url` function:

```python
from typing import NamedTuple


class MachineRow(NamedTuple):
    name: str
    url: str
    online: bool
    is_self: bool


def plan_launcher(
    status: dict | None,
    *,
    tag: str,
    self_dnsname: str | None,
) -> list[MachineRow]:
    """Select tagged peers + Self from parsed tailscale status JSON.

    Returns [] when status is unavailable. Self is always included when
    self_dnsname matches Self.DNSName (regardless of whether Self is tagged).
    Peers are included only if tag appears in their Tags list.
    Sort: self-first, then online-first, then by name.
    """
    if status is None:
        return []

    rows: list[MachineRow] = []

    # Self — always included if self_dnsname matches, regardless of tags
    self_node = status.get("Self") or {}
    self_dns = (self_node.get("DNSName") or "").rstrip(".")
    if self_dnsname and self_dns == self_dnsname.rstrip("."):
        rows.append(MachineRow(
            name=self_node.get("HostName", ""),
            url=_dashboard_url(self_node["DNSName"]),
            online=bool(self_node.get("Online")),
            is_self=True,
        ))

    # Peers — only tagged ones
    for peer in (status.get("Peer") or {}).values():
        if tag not in peer.get("Tags", []):
            continue
        rows.append(MachineRow(
            name=peer.get("HostName", ""),
            url=_dashboard_url(peer["DNSName"]),
            online=bool(peer.get("Online")),
            is_self=False,
        ))

    rows.sort(key=lambda r: (not r.is_self, not r.online, r.name))
    return rows
```

Also update the module docstring at the top of `tailnet.py` — replace the "Phase 3 (launcher) adds plan_launcher() here" future-tense note with a present-tense description, and move the `from typing import NamedTuple` import to the top of the file alongside `from __future__ import annotations`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tailnet.py -v`
Expected: all tests PASS (existing + new)

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/pytest --tb=short -q`
Expected: all ~1920 tests pass, no regressions

- [ ] **Step 6: Commit**

```bash
git add crr/core/tailnet.py tests/test_tailnet.py
git commit -m "feat(launcher): add plan_launcher() — pure machine list from tailnet status"
```

---

### Task 2: Machines contract in `crr/core/contracts.py`

**Files:**
- Modify: `crr/core/contracts.py`
- Test: `tests/test_contracts.py`

**Interfaces:**
- Consumes: `MachineRow` fields from Task 1 (determines key names)
- Produces:
  - `MACHINES_CONTRACT_VERSION = 1`
  - `MACHINE_ROW_KEYS = ("name", "url", "online", "is_self")`
  - `MACHINES_PAYLOAD_KEYS = ("contract", "machines")`
  - `validate_machines_payload(payload: Any) -> None`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contracts.py`:

```python
def test_machines_contract_version_is_1():
    assert contracts.MACHINES_CONTRACT_VERSION == 1


def test_valid_machines_payload_passes():
    contracts.validate_machines_payload({
        "contract": 1,
        "machines": [
            {"name": "Lovelace", "url": "https://lovelace.ts.net/", "online": True, "is_self": False},
        ],
    })


def test_machines_payload_missing_key_rejected():
    with pytest.raises(contracts.ContractError, match="missing key"):
        contracts.validate_machines_payload({
            "contract": 1,
            "machines": [
                {"name": "Lovelace", "url": "https://lovelace.ts.net/", "online": True},
            ],
        })


def test_machines_payload_unknown_key_rejected():
    with pytest.raises(contracts.ContractError, match="unknown key"):
        contracts.validate_machines_payload({
            "contract": 1,
            "machines": [
                {"name": "Lovelace", "url": "https://lovelace.ts.net/",
                 "online": True, "is_self": False, "extra": 1},
            ],
        })


def test_machines_payload_wrong_version_rejected():
    with pytest.raises(contracts.ContractError, match="contract"):
        contracts.validate_machines_payload({
            "contract": 99,
            "machines": [],
        })


def test_machines_payload_empty_list_passes():
    contracts.validate_machines_payload({"contract": 1, "machines": []})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_contracts.py -v -k "machines"`
Expected: FAIL — `AttributeError: module 'crr.core.contracts' has no attribute 'MACHINES_CONTRACT_VERSION'`

- [ ] **Step 3: Implement machines contract**

In `crr/core/contracts.py`:

1. Add after `ACTION_CONTRACT_VERSION = 1` (around line 81):

```python
MACHINES_CONTRACT_VERSION = 1
```

2. Add after the existing key tuples (after `EXCLUSIONS_PAYLOAD_KEYS` or similar):

```python
MACHINE_ROW_KEYS = ("name", "url", "online", "is_self")
MACHINES_PAYLOAD_KEYS = ("contract", "machines")
```

3. Add a validator function at the end of the file, following the `validate_sessions_payload` pattern:

```python
def validate_machines_payload(payload: Any) -> None:
    """Raise ContractError unless ``payload`` is a valid /api/machines body."""
    payload = _require_mapping(payload, "/api/machines payload")
    _require_exact_keys(payload, MACHINES_PAYLOAD_KEYS, "/api/machines payload")
    _require_type(payload["contract"], int, "/api/machines 'contract'")
    if payload["contract"] != MACHINES_CONTRACT_VERSION:
        raise ContractError(
            f"/api/machines 'contract' is {payload['contract']}, "
            f"this build serves {MACHINES_CONTRACT_VERSION}"
        )
    _require_type(payload["machines"], list, "/api/machines 'machines'")
    for row in payload["machines"]:
        row = _require_mapping(row, "/api/machines row")
        _require_exact_keys(row, MACHINE_ROW_KEYS, "/api/machines row")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_contracts.py -v -k "machines"`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/pytest --tb=short -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add crr/core/contracts.py tests/test_contracts.py
git commit -m "feat(launcher): add machines contract — MACHINE_ROW_KEYS + validator"
```

---

### Task 3: Config `launcher_tag` default + version bump

**Files:**
- Modify: `crr/core/config.py`
- Test: `tests/test_config.py` (add a pinning test)

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `cfg.DEFAULTS["launcher_tag"]` = `"tag:crr"`, `CONFIG_DEFAULTS_VERSION` = 20

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_launcher_tag_default():
    assert cfg.DEFAULTS["launcher_tag"] == "tag:crr"
    assert cfg.Config().get("launcher_tag") == "tag:crr"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py::test_launcher_tag_default -v`
Expected: FAIL — `KeyError: 'launcher_tag'`

- [ ] **Step 3: Add `launcher_tag` to DEFAULTS and bump version**

In `crr/core/config.py`:

1. Add a `# v20:` ledger comment before the `CONFIG_DEFAULTS_VERSION` line:

```python
# v20: added launcher_tag (spec 2026-08-18 — Phase 3 Launcher: the Tailscale
# tag used to discover peer machines for the Machines panel; default tag:crr)
CONFIG_DEFAULTS_VERSION = 20
```

(Change the `= 19` to `= 20` on the existing line.)

2. Add to the `DEFAULTS` dict (at the end, or in a logical group):

```python
    # launcher (Phase 3)
    "launcher_tag": "tag:crr",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_config.py::test_launcher_tag_default -v`
Expected: PASS

- [ ] **Step 5: Run the version ledger test**

Run: `.venv/bin/pytest tests/test_version_ledger.py -v`
Expected: PASS — the `# v20:` entry makes the ledger contiguous from 2 to 20.

- [ ] **Step 6: Run full suite**

Run: `.venv/bin/pytest --tb=short -q`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add crr/core/config.py tests/test_config.py
git commit -m "feat(launcher): add launcher_tag config default (tag:crr)"
```

---

### Task 4: `/api/machines` route + `machines_provider` wiring

**Files:**
- Modify: `crr/core/web.py` (route)
- Modify: `crr/cli.py` (provider closure + wiring through `make_web_handler` → `_dispatch`)
- Test: `tests/test_web.py` (route tests)

**Interfaces:**
- Consumes:
  - `tailnet.plan_launcher(status, tag=..., self_dnsname=...)` from Task 1
  - `contracts.validate_machines_payload(payload)` from Task 2
  - `config.get("launcher_tag")` from Task 3
  - `ts_adapter.status()` from existing `crr/adapters/tailscale.py`
- Produces:
  - GET `/api/machines` route returning `{"contract": 1, "machines": [...]}`
  - `machines_provider` kwarg threaded through all five wiring sites

There are **five wiring sites** for `machines_provider` — mirror the existing `qr_svg_provider` pattern exactly:

1. `web.py` `handle_request` signature: `machines_provider: Callable[[], dict] | None = None`
2. `web.py` route: `if path == "/api/machines":` block (lazy, 404-without-provider)
3. `cli.py` `make_web_handler` signature: `machines_provider: Callable[[], dict] | None = None`
4. `cli.py` `_dispatch` pass-through: `machines_provider=machines_provider`
5. `cli.py` `_cmd_web` closure + handler call: closure definition + `machines_provider=machines_provider` kwarg

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`. Use the existing `_handle()` helper pattern:

```python
def test_machines_route_returns_json():
    payload = {"contract": 1, "machines": [
        {"name": "Lovelace", "url": "https://lovelace.ts.net/", "online": True, "is_self": False},
    ]}
    r = _handle("GET", "/api/machines", machines_provider=lambda: payload)
    assert r.status == 200
    assert r.content_type == "application/json"
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -v -k "machines"`
Expected: FAIL — `_handle()` does not accept `machines_provider` kwarg / route not found

- [ ] **Step 3a: Add route to `web.py`**

In `crr/core/web.py`:

1. Add `machines_provider: Callable[[], dict[str, Any]] | None = None,` to the `handle_request` function signature, after `qr_svg_provider`.

2. Add the route block inside the `if method == "GET":` section, after the `/qr.svg` block and before the PWA routes:

```python
        if path == "/api/machines":
            if machines_provider is None:
                return _plain(404, "not found")
            return _json(200, machines_provider())
```

- [ ] **Step 3b: Wire `machines_provider` through `cli.py`**

In `crr/cli.py`:

1. Add `machines_provider: Callable[[], dict] | None = None,` to the `make_web_handler` function signature (after `qr_svg_provider`, around line 3614).

2. Add `machines_provider=machines_provider,` to the `_dispatch` method's `web.handle_request` call (after `qr_svg_provider=qr_svg_provider,`, around line 3648).

3. Add the `machines_provider` closure in `_cmd_web`, after the `qr_svg_provider` closure (around line 3998):

```python
    def machines_provider() -> dict:
        status = ts_adapter.status()
        self_dns = ((status or {}).get("Self") or {}).get("DNSName")
        rows = tailnet.plan_launcher(status, tag=config.get("launcher_tag"), self_dnsname=self_dns)
        payload = {
            "contract": contracts.MACHINES_CONTRACT_VERSION,
            "machines": [row._asdict() for row in rows],
        }
        contracts.validate_machines_payload(payload)
        return payload
```

4. Add `machines_provider=machines_provider,` to the `make_web_handler(...)` call (after `qr_svg_provider=qr_svg_provider,`, around line 4234).

5. Add `from crr.core import contracts` to the imports at the top of `cli.py` if not already there (it likely is; check first).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -v -k "machines"`
Expected: PASS

- [ ] **Step 5: Run import linter**

Run: `.venv/bin/python -m importlinter --config pyproject.toml`
Expected: PASS — the one-way layering contract is maintained (cli → adapters → core, web.py is core, cli.py is cli-layer).

- [ ] **Step 6: Run full suite**

Run: `.venv/bin/pytest --tb=short -q`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add crr/core/web.py crr/cli.py tests/test_web.py
git commit -m "feat(launcher): add /api/machines route with machines_provider wiring"
```

---

### Task 5: Machines panel in `page.html` + `PAGE_VERSION` bump

**Files:**
- Modify: `crr/core/page.html` (button, panel, JS)
- Modify: `crr/core/web.py` (`PAGE_VERSION` 56 → 57)
- Modify: `tests/test_page_version_guard.py` (add v57 pin)

**Interfaces:**
- Consumes: GET `/api/machines` returning `{"contract": 1, "machines": [...]}`
- Produces: visual Machines panel in the dashboard

- [ ] **Step 1: Add the "Machines" button to the `#tools` div**

In `crr/core/page.html`, inside the `<div id="tools">` section (after the existing buttons like `adddev-btn`, around line 387), add:

```html
  <button id="machines-btn" type="button" title="All crr dashboards on your tailnet.">Machines</button>
```

- [ ] **Step 2: Add the hidden panel div**

After the `<div id="adddev-box" ...>` closing tag (around line 395), add:

```html
<div id="machines-panel" hidden></div>
```

- [ ] **Step 3: Add CSS for the machines panel**

In the `<style>` block, add:

```css
  #machines-panel { margin: 10px 0; padding: 10px; background: #1e1e1e; border-radius: 6px; font-size: 13px; }
  #machines-panel .machine-row { display: flex; align-items: center; padding: 6px 0; border-bottom: 1px solid #2a2a2a; }
  #machines-panel .machine-row:last-child { border-bottom: none; }
  #machines-panel .machine-name { flex: 1; }
  #machines-panel .machine-name a { color: #7cacf8; text-decoration: none; }
  #machines-panel .machine-name a:hover { text-decoration: underline; }
  #machines-panel .machine-badge { font-size: 11px; padding: 2px 6px; border-radius: 3px; }
  #machines-panel .badge-online { background: #1a3a2a; color: #6fcf97; }
  #machines-panel .badge-offline { background: #3a2a1a; color: #cf9f6f; }
  #machines-panel .machine-self { font-size: 11px; color: #8a93a2; margin-left: 6px; }
  #machines-panel .machines-note { color: #8a93a2; font-style: italic; }
```

- [ ] **Step 4: Add the JavaScript for lazy fetch + render**

In the `<script>` block, add (after the `adddev-btn` click handler, around line 1312):

```javascript
var machinesLoaded = false;
document.getElementById("machines-btn").addEventListener("click", function () {
  var panel = document.getElementById("machines-panel");
  if (!panel.hidden) { panel.hidden = true; return; }
  panel.hidden = false;
  if (machinesLoaded) { return; }
  panel.textContent = "loading…";
  fetch("/api/machines")
    .then(function (r) { return r.json(); })
    .then(function (d) { renderMachines(d); machinesLoaded = true; })
    .catch(function () { panel.textContent = "machines unavailable"; });
});

function renderMachines(data) {
  var panel = document.getElementById("machines-panel");
  panel.textContent = "";
  var machines = data.machines || [];
  var hasTaggedPeer = machines.some(function (m) { return !m.is_self; });

  if (machines.length === 0) {
    var note = document.createElement("div");
    note.className = "machines-note";
    note.textContent = "no tag:crr machines found — see setup";
    panel.appendChild(note);
    return;
  }

  machines.forEach(function (m) {
    var row = document.createElement("div");
    row.className = "machine-row";

    var nameDiv = document.createElement("div");
    nameDiv.className = "machine-name";
    var link = document.createElement("a");
    link.setAttribute("href", m.url);
    link.textContent = m.name;
    nameDiv.appendChild(link);
    if (m.is_self) {
      var selfTag = document.createElement("span");
      selfTag.className = "machine-self";
      selfTag.textContent = "(this machine)";
      nameDiv.appendChild(selfTag);
    }
    row.appendChild(nameDiv);

    var badge = document.createElement("span");
    badge.className = "machine-badge " + (m.online ? "badge-online" : "badge-offline");
    badge.textContent = m.online ? "on tailnet" : "offline";
    row.appendChild(badge);

    panel.appendChild(row);
  });

  if (!hasTaggedPeer) {
    var note = document.createElement("div");
    note.className = "machines-note";
    note.textContent = "no tag:crr machines found — see setup";
    panel.appendChild(note);
  }
}
```

- [ ] **Step 5: Verify JavaScript syntax**

Run: `node --check <(grep -A 9999 '<script>' crr/core/page.html | grep -B 9999 '</script>' | head -n -1 | tail -n +2)`

If that doesn't work with process substitution, extract the script block to a temp file and run `node --check` on it. Expected: no syntax errors.

- [ ] **Step 6: Bump `PAGE_VERSION` in `web.py`**

In `crr/core/web.py`, change:

```python
PAGE_VERSION = 56  # v56: PWA installability — manifest, icons, SW registration
```

to:

```python
PAGE_VERSION = 57  # v57: Machines panel — tag:crr peer list with on-tailnet badge
```

- [ ] **Step 7: Add the v57 pin to `test_page_version_guard.py`**

In `tests/test_page_version_guard.py`, add a new entry to `PAGE_PINS`. First compute the sha256:

```bash
python3 -c "import hashlib; print(hashlib.sha256(open('crr/core/page.html','rb').read()).hexdigest())"
```

Then add the result as `57: "<sha256>",` to the `PAGE_PINS` dict (at the top, before the `56:` entry).

- [ ] **Step 8: Run the page version guard test**

Run: `.venv/bin/pytest tests/test_page_version_guard.py -v`
Expected: PASS

- [ ] **Step 9: Run full suite**

Run: `.venv/bin/pytest --tb=short -q`
Expected: all tests pass, no regressions

- [ ] **Step 10: Commit**

```bash
git add crr/core/page.html crr/core/web.py tests/test_page_version_guard.py
git commit -m "feat(launcher): add Machines panel — tag:crr peer list with on-tailnet badge"
```

---

## Acceptance gate (manual, at deploy time)

On **lovelace** (a clean single node, not the WSL host):
1. Add `tagOwners` for `tag:crr` in the tailnet ACL
2. `tailscale set --advertise-tags=tag:crr`
3. Confirm `tailscale status --json` from another node shows `Tags: ["tag:crr"]` on lovelace's peer object
4. Open the dashboard, expand "Machines" — lovelace should appear with an "on tailnet" badge
5. The link should navigate to lovelace's own dashboard

This is the analog of the Phase 2 phone-scan acceptance test.
