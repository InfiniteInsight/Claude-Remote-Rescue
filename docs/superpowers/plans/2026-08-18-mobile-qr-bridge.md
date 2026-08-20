# Mobile QR Bridge Implementation Plan (Phase 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `crr qr` prints a scannable QR code of this machine's tailnet dashboard URL in the terminal (and at the end of `bootstrap.sh`), plus a `/qr.svg` route and a small dashboard "Add a device" affordance — so a phone gets onto the dashboard with a scan instead of typed URL.

**Architecture:** A vendored, zero-pip-dependency QR library (segno) under `crr/vendor/`, wrapped by a pure `crr/core/qr.py`. A new cross-platform `crr/adapters/tailscale.py` reads `tailscale status --json` / `tailscale serve status --json` (tri-state degrade). A pure `crr/core/tailnet.py` derives this machine's dashboard URL. The cli composes them for `crr qr`; the web layer serves `/qr.svg`.

**Tech Stack:** Python 3.11+ stdlib, vendored `segno` (BSD, pure-Python, zero deps), `tailscale` CLI (subprocess), stdlib `http` web server.

**Spec:** `docs/superpowers/specs/2026-08-18-mobile-onboarding-bridge-design.md`

## Global Constraints

- **Zero runtime dependencies.** segno is *vendored* (copied into the tree, no pip runtime dep). `pyproject.toml` `dependencies = []` stays empty.
- **One-way layering** (`.importlinter`, CI-enforced `lint-imports`): `crr.cli → crr.adapters → crr.core`. `crr.core` imports neither adapters nor cli. `crr.vendor` is contract-neutral (unlisted) and must import nothing from `crr.*`.
- **Adapter tri-state:** subprocess wrappers return `None` on missing binary / timeout / `OSError` / nonzero exit / unparseable output — never raise. Mirror `crr/adapters/tmux.py`.
- **Untrusted fields to the DOM via `textContent`/`createTextNode` only**; the QR affordance renders a server-built same-origin URL only.
- **PAGE_VERSION** (`crr/core/web.py:43`, currently `53`) is bumped on every `page.html` change, and a new `sha256` entry is APPENDED to `PAGE_PINS` in `tests/test_page_version_guard.py`.
- **Test-first.** No test runs real `tailscale`, execs, or hits the network.
- **URL rule:** dashboard URLs are built from a node's `DNSName` (trailing dot stripped), **never** `HostName`.

---

### Task 1: Vendor segno + `crr/core/qr.py` wrapper

**Files:**
- Create: `crr/vendor/__init__.py` (empty), `crr/vendor/segno/…` (copied package)
- Create: `crr/core/qr.py`
- Modify: `pyproject.toml` (package discovery + package-data)
- Test: `tests/test_qr.py`

**Interfaces:**
- Produces: `crr.core.qr.to_terminal(text: str) -> str`, `crr.core.qr.to_svg(text: str) -> str`, `crr.core.qr.make(text: str)` (returns the vendored `segno.QRCode`; internal helper).

- [ ] **Step 1: Vendor segno**

Obtain the segno source (latest stable, 1.6.x) WITHOUT installing it as a dependency, and copy only its package directory into the tree:

```bash
# From repo root. Download the sdist, extract, copy the pure-python package.
python -m pip download --no-deps --no-binary :all: segno -d /tmp/segno-src
tar -xf /tmp/segno-src/segno-*.tar.gz -C /tmp/segno-src
mkdir -p crr/vendor
cp -r /tmp/segno-src/segno-*/src/segno crr/vendor/segno   # segno ships under src/segno
: > crr/vendor/__init__.py
```

If segno's sdist layout differs (older versions place `segno/` at the root, not under `src/`), copy whichever directory contains `segno/__init__.py`. Do NOT copy tests, docs, or `setup.py`.

- [ ] **Step 2: Make segno's internal imports resolve under `crr.vendor`**

segno imports its own submodules. Vendored, those must resolve as `crr.vendor.segno.*`. Convert any absolute self-imports to relative so the package is location-independent:

- `import segno.writers` / `from segno import writers` → `from . import writers`
- `from segno.encoder import …` → `from .encoder import …`

Grep to find them and fix each:

```bash
grep -rn "segno" crr/vendor/segno/*.py | grep -E "import|from" | grep -v "^\s*#"
```

Add a one-line header comment to `crr/vendor/segno/__init__.py`:
```python
# Vendored from segno (BSD-3-Clause) — pure-Python, zero-dependency QR encoder.
# Do not edit except to keep internal imports relative. See crr/core/qr.py.
```

- [ ] **Step 3: Verify the vendored import works and produces output**

```bash
python -c "from crr.vendor import segno; import io; \
b=io.StringIO(); segno.make('https://x.ts.net/', error='m').terminal(out=b, compact=True); \
print(len(b.getvalue()), 'chars'); \
print(segno.make('https://x.ts.net/', error='m').svg_inline()[:40])"
```
Expected: a non-zero char count and an SVG string beginning with `<svg`. If `svg_inline` is unavailable in the vendored version, use `save(BytesIO(), kind='svg')` (see wrapper below) — note which API exists.

- [ ] **Step 4: Write the failing wrapper test**

`tests/test_qr.py`:
```python
from crr.core import qr


def test_to_terminal_is_nonempty_block_text():
    out = qr.to_terminal("https://lovelace.tail3af2d9.ts.net/")
    assert isinstance(out, str) and out.strip()
    # QR terminal art is drawn with block glyphs / spaces; assert it's a grid.
    assert any(ch in out for ch in ("█", "▀", "▄", "█", " "))
    assert "\n" in out  # multiple rows


def test_to_svg_is_an_svg_document():
    out = qr.to_svg("https://lovelace.tail3af2d9.ts.net/")
    assert isinstance(out, str)
    assert "<svg" in out and "</svg>" in out


def test_encoding_is_deterministic():
    url = "https://lovelace.tail3af2d9.ts.net/"
    assert qr.to_terminal(url) == qr.to_terminal(url)
    assert qr.to_svg(url) == qr.to_svg(url)


def test_distinct_inputs_differ():
    assert qr.to_svg("https://a.ts.net/") != qr.to_svg("https://b.ts.net/")
```

- [ ] **Step 5: Run it, verify it fails**

Run: `.venv/bin/pytest tests/test_qr.py -v`
Expected: FAIL — `ModuleNotFoundError: crr.core.qr` (module not yet created).

- [ ] **Step 6: Write `crr/core/qr.py`**

```python
"""Pure QR rendering for the dashboard URL — thin wrapper over vendored segno.

segno owns the QR matrix and both renderers; this module only adapts its API
to the two shapes crr needs (terminal art for `crr qr`, an SVG string for the
web `/qr.svg` route) and pins the error-correction level. No I/O, no globals.
"""

from __future__ import annotations

import io

from crr.vendor import segno

# Level "M" (~15% recovery) comfortably fits a tailnet URL and scans well off
# a screen; the QR stays a low version (small, quick to scan).
_ERROR = "m"


def make(text: str):
    """The vendored segno.QRCode for ``text`` (internal; renderers use it)."""
    return segno.make(text, error=_ERROR)


def to_terminal(text: str) -> str:
    """Scannable QR as terminal block art (quiet-zone border included)."""
    buf = io.StringIO()
    make(text).terminal(out=buf, compact=True)
    return buf.getvalue()


def to_svg(text: str) -> str:
    """Scannable QR as a self-contained SVG document string."""
    buf = io.BytesIO()
    make(text).save(buf, kind="svg", scale=6, border=4)
    return buf.getvalue().decode("utf-8")
```

If `terminal(compact=True)` is unsupported in the vendored version, drop `compact=True`. If `save(kind="svg")` emits an XML declaration you don't want, that's fine — the test only requires `<svg>…</svg>` to be present.

- [ ] **Step 7: Add vendored package to build config**

`pyproject.toml` — ensure `crr.vendor` and `crr.vendor.segno` are discovered and segno's data (if any) ships. Under `[tool.setuptools.packages.find]` confirm the include covers `crr*` (it should already). Under `[tool.setuptools.package-data]`, add any non-`.py` files segno needs (segno is pure `.py`, so typically none — verify with `ls crr/vendor/segno`).

- [ ] **Step 8: Run tests + lint**

Run: `.venv/bin/pytest tests/test_qr.py -v && .venv/bin/lint-imports`
Expected: PASS, and `lint-imports` prints `crr one-way layering (cli -> adapters -> core) KEPT`. (crr.core importing crr.vendor is contract-neutral.)

- [ ] **Step 9: Commit**

```bash
git add crr/vendor crr/core/qr.py tests/test_qr.py pyproject.toml
git commit -m "feat(core): vendored segno + qr.py (terminal + svg renderers)"
```

---

### Task 2: `crr/adapters/tailscale.py` — status + serve_status (tri-state)

**Files:**
- Create: `crr/adapters/tailscale.py`
- Test: `tests/test_adapters.py` (append)

**Interfaces:**
- Produces: `crr.adapters.tailscale.RealTailscale(timeout: float)` with `available() -> bool`, `status() -> dict | None`, `serve_status() -> dict | None`. Pure builders `_status_cmd() -> list[str]`, `_serve_status_cmd() -> list[str]`.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing tests**

`tests/test_adapters.py` (append; follow the file's existing import style):
```python
from crr.adapters import tailscale


def test_tailscale_status_cmd_is_json():
    assert tailscale._status_cmd() == ["tailscale", "status", "--json"]


def test_tailscale_serve_status_cmd_is_json():
    assert tailscale._serve_status_cmd() == ["tailscale", "serve", "status", "--json"]


def test_status_parses_json(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(
        tailscale.subprocess, "run",
        lambda *a, **k: _fake_proc(0, '{"Self": {"DNSName": "n.ts.net."}}'))
    assert ts.status() == {"Self": {"DNSName": "n.ts.net."}}


def test_status_none_when_binary_absent(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: None)
    assert ts.status() is None


def test_status_none_on_nonzero(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", lambda *a, **k: _fake_proc(1, ""))
    assert ts.status() is None


def test_status_none_on_garbage(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", lambda *a, **k: _fake_proc(0, "not json"))
    assert ts.status() is None


def test_status_none_on_timeout(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    def _boom(*a, **k):
        raise tailscale.subprocess.TimeoutExpired(cmd="tailscale", timeout=2.0)
    monkeypatch.setattr(tailscale.subprocess, "run", _boom)
    assert ts.status() is None


class _fake_proc:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/bin/pytest tests/test_adapters.py -k tailscale -v`
Expected: FAIL — `ModuleNotFoundError: crr.adapters.tailscale`.

- [ ] **Step 3: Write the adapter (mirror `crr/adapters/tmux.py`)**

`crr/adapters/tailscale.py`:
```python
"""Tailscale CLI adapter — reads `tailscale status`/`serve status` as JSON.

Mirrors crr/adapters/tmux.py: a class holding a timeout, pure command
builders, and tri-state wrappers that return None on missing binary /
timeout / OSError / nonzero exit / unparseable output (never raise). All
interpretation of the parsed JSON lives in pure core (crr.core.tailnet).
"""

from __future__ import annotations

import json
import shutil
import subprocess


def _status_cmd() -> list[str]:
    return ["tailscale", "status", "--json"]


def _serve_status_cmd() -> list[str]:
    return ["tailscale", "serve", "status", "--json"]


class RealTailscale:
    def __init__(self, timeout: float) -> None:
        self._timeout = timeout

    def available(self) -> bool:
        return shutil.which("tailscale") is not None

    def _run_json(self, argv: list[str]) -> dict | None:
        if shutil.which("tailscale") is None:
            return None
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def status(self) -> dict | None:
        return self._run_json(_status_cmd())

    def serve_status(self) -> dict | None:
        return self._run_json(_serve_status_cmd())
```

Note: `tailscale serve status --json` may exit nonzero or emit non-dict output when serve is unconfigured — both collapse to `None` here, which the core treats as "serve not live." That is the intended degrade.

- [ ] **Step 4: Run tests + full suite + lint**

Run: `.venv/bin/pytest tests/test_adapters.py -k tailscale -v && .venv/bin/lint-imports`
Expected: PASS; contract KEPT.

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/tailscale.py tests/test_adapters.py
git commit -m "feat(adapters): tailscale status/serve_status (tri-state JSON reader)"
```

---

### Task 3: `crr/core/tailnet.py` — `self_dashboard_url` (pure)

**Files:**
- Create: `crr/core/tailnet.py`
- Test: `tests/test_tailnet.py`

**Interfaces:**
- Consumes: parsed dicts shaped like `tailscale status --json` (`{"Self": {"DNSName": "..."}}`) and `tailscale serve status --json`.
- Produces: `crr.core.tailnet.self_dashboard_url(status: dict | None, serve_status: dict | None) -> str | None`. (Phase 3 will add `plan_launcher` to this same module.)

**Behavior:** Return `None` if `status` is `None`, if `Self.DNSName` is missing/empty, **or if `serve_status` is `None`/empty** (serve not live → no working https URL). Otherwise return `https://<dnsname>/` with the trailing dot stripped from `DNSName`.

- [ ] **Step 1: Write the failing test**

`tests/test_tailnet.py`:
```python
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
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/bin/pytest tests/test_tailnet.py -v`
Expected: FAIL — `ModuleNotFoundError: crr.core.tailnet`.

- [ ] **Step 3: Write `crr/core/tailnet.py`**

```python
"""Pure selection/URL logic over parsed `tailscale status` JSON.

No subprocess, no I/O — the crr.adapters.tailscale adapter supplies the dicts.
Phase 3 (launcher) adds plan_launcher() here alongside self_dashboard_url().
"""

from __future__ import annotations


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
```

- [ ] **Step 4: Run tests + lint**

Run: `.venv/bin/pytest tests/test_tailnet.py -v && .venv/bin/lint-imports`
Expected: PASS; contract KEPT.

- [ ] **Step 5: Commit**

```bash
git add crr/core/tailnet.py tests/test_tailnet.py
git commit -m "feat(core): tailnet.self_dashboard_url (pure, serve-gated)"
```

---

### Task 4: `crr qr` cli command

**Files:**
- Modify: `crr/cli.py` (new `_cmd_qr` handler; register subparser in `_build_parser`)
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `crr.core.qr.to_terminal`, `crr.core.tailnet.self_dashboard_url`, `crr.adapters.tailscale.RealTailscale`, config key `interop_timeout_seconds`, config key `dashboard_port`.
- Produces: `crr qr` subcommand; handler `_cmd_qr(args) -> int`.

**Behavior:** Resolve the dashboard URL from a `RealTailscale`. On success, print the terminal QR + the URL beneath, return 0. On degrade (URL `None`), print the loopback URL and a one-line hint to run `tailscale serve --bg <port>`, return 0 (informational, not an error). Inject the adapter through a module-level seam so tests substitute a fake and no test runs real tailscale.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py` (append; match the file's fixture/capsys conventions):
```python
def test_qr_prints_code_and_url(monkeypatch, capsys):
    import crr.cli as cli

    class _FakeTS:
        def __init__(self, *_a): pass
        def status(self): return {"Self": {"DNSName": "lovelace.tail3af2d9.ts.net."}}
        def serve_status(self): return {"TCP": {"443": {"HTTPS": True}}}

    monkeypatch.setattr(cli.tailscale, "RealTailscale", _FakeTS)
    rc = cli._cmd_qr(_ns())  # _ns(): the file's helper for an argparse.Namespace
    out = capsys.readouterr().out
    assert rc == 0
    assert "https://lovelace.tail3af2d9.ts.net/" in out
    assert out.strip()  # a QR was rendered above the URL


def test_qr_degrades_with_hint_when_serve_not_live(monkeypatch, capsys):
    import crr.cli as cli

    class _FakeTS:
        def __init__(self, *_a): pass
        def status(self): return {"Self": {"DNSName": "lovelace.tail3af2d9.ts.net."}}
        def serve_status(self): return None  # serve not configured

    monkeypatch.setattr(cli.tailscale, "RealTailscale", _FakeTS)
    rc = cli._cmd_qr(_ns())
    out = capsys.readouterr().out
    assert rc == 0
    assert "tailscale serve" in out           # the hint
    assert "127.0.0.1" in out or "loopback" in out
```
If the file has no `_ns()` helper, build the namespace inline: `argparse.Namespace()` (the handler reads no args).

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -k qr -v`
Expected: FAIL — `AttributeError: module 'crr.cli' has no attribute '_cmd_qr'`.

- [ ] **Step 3: Add imports + handler + subparser**

In `crr/cli.py`: ensure `from crr.adapters import … tailscale` is in the adapters import line (`cli.py:43`), and `from crr.core import … qr, tailnet` in the core imports. Add the handler (near the other `_cmd_*`):
```python
def _cmd_qr(args: argparse.Namespace) -> int:
    config = _load_config()
    ts = tailscale.RealTailscale(config.get("interop_timeout_seconds"))
    url = tailnet.self_dashboard_url(ts.status(), ts.serve_status())
    if url is None:
        port = config.get("dashboard_port")
        print(f"http://127.0.0.1:{port}/  (loopback only)")
        print(f"To reach it from your phone, run:  tailscale serve --bg {port}")
        return 0
    print(qr.to_terminal(url))
    print(url)
    return 0
```
Use whatever the file's canonical config loader is (match how `_cmd_web`/`_cmd_doctor` obtain `config` — likely `config = _load_config()` or `cfg.load(...)`; copy the exact call they use).

Register it in `_build_parser` alongside the others:
```python
qrp = sub.add_parser("qr", help="print a scannable QR of this machine's dashboard URL")
qrp.set_defaults(func=_cmd_qr)
```

- [ ] **Step 4: Run tests + full suite + lint**

Run: `.venv/bin/pytest tests/test_cli.py -k qr -v && .venv/bin/lint-imports`
Expected: PASS; contract KEPT.

- [ ] **Step 5: Add `crr qr` to the Commands table in README**

`README.md` Commands table — add a row:
```
| `crr qr` | Print a scannable QR code of this machine's tailnet dashboard URL (scan it to open the dashboard on your phone) |
```

- [ ] **Step 6: Commit**

```bash
git add crr/cli.py tests/test_cli.py README.md
git commit -m "feat(cli): crr qr — scannable QR of this machine's dashboard URL"
```

---

### Task 5: web `/qr.svg` route + dashboard "Add a device" affordance

**Files:**
- Modify: `crr/core/web.py` (new `qr_svg_provider` param + `/qr.svg` GET route; PAGE_VERSION bump)
- Modify: `crr/cli.py` (`make_web_handler` new param; `_cmd_web` closure; thread through `_dispatch`)
- Modify: `crr/core/page.html` (an "Add a device" button revealing an `<img src="/qr.svg">`)
- Test: `tests/test_web.py`, `tests/test_page_version_guard.py`

**Interfaces:**
- Consumes: `crr.core.qr.to_svg`, `crr.core.tailnet.self_dashboard_url`, the `RealTailscale` adapter already constructed in `_cmd_web`.
- Produces: `handle_request(..., qr_svg_provider=None)`; GET `/qr.svg` → `image/svg+xml` (200) or `404` when provider is `None`/returns `None`.

- [ ] **Step 1: Write the failing web tests**

`tests/test_web.py` (append; match existing `handle_request` call style):
```python
def test_qr_svg_route_returns_svg():
    resp = web.handle_request(
        "GET", "/qr.svg", {"host": "localhost"},
        sessions_provider=lambda: {},
        qr_svg_provider=lambda: "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        allowed_hosts={"localhost"}, allowed_suffixes=(".ts.net",),
    )
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/svg+xml"
    assert b"<svg" in resp.body


def test_qr_svg_route_404_without_provider():
    resp = web.handle_request(
        "GET", "/qr.svg", {"host": "localhost"},
        sessions_provider=lambda: {},
        qr_svg_provider=None,
        allowed_hosts={"localhost"}, allowed_suffixes=(".ts.net",),
    )
    assert resp.status == 404


def test_qr_svg_route_404_when_provider_returns_none():
    resp = web.handle_request(
        "GET", "/qr.svg", {"host": "localhost"},
        sessions_provider=lambda: {},
        qr_svg_provider=lambda: None,   # serve not live
        allowed_hosts={"localhost"}, allowed_suffixes=(".ts.net",),
    )
    assert resp.status == 404
```
Copy the exact keyword names/positional order `handle_request` uses from a nearby existing test — the snippet above assumes `sessions_provider`, `allowed_hosts`, `allowed_suffixes` keywords; align to reality.

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/bin/pytest tests/test_web.py -k qr_svg -v`
Expected: FAIL — `handle_request() got an unexpected keyword argument 'qr_svg_provider'`.

- [ ] **Step 3: Add the param + route in `crr/core/web.py`**

Add `qr_svg_provider: Callable[[], str | None] | None = None` to `handle_request`'s keyword params (`web.py:228-257`). In the `if method == "GET":` block, before the `return _plain(404, "not found")` fallthrough (`web.py:348`):
```python
        if path == "/qr.svg":
            if qr_svg_provider is None:
                return _plain(404, "not found")
            svg = qr_svg_provider()
            if svg is None:
                return _plain(404, "not found")
            return _resp(200, "image/svg+xml", svg.encode("utf-8"))
```

- [ ] **Step 4: Thread it through `crr/cli.py`**

Add a matching `qr_svg_provider=None` param to `make_web_handler` (`cli.py:3577`), forward it in the `web.handle_request(...)` call inside `_dispatch` (`cli.py:3611-3634`). In `_cmd_web`, near the other closures, define (the `RealTailscale` adapter is constructed here alongside `tmux.RealTmux` at `cli.py:3958`):
```python
    ts_adapter = tailscale.RealTailscale(config.get("interop_timeout_seconds"))

    def qr_svg_provider() -> str | None:
        url = tailnet.self_dashboard_url(ts_adapter.status(), ts_adapter.serve_status())
        return qr.to_svg(url) if url else None
```
and pass `qr_svg_provider=qr_svg_provider` in the `make_web_handler(...)` call (`cli.py:4187`).

- [ ] **Step 5: Add the dashboard affordance in `page.html`**

Add a small button near the other panel triggers (`page.html:368-369`) and a hidden container:
```html
<button id="adddev-btn" class="ghost">📱 Add a device</button>
<div id="adddev-box" hidden>
  <p>Scan to open this dashboard on another device:</p>
  <img id="adddev-qr" alt="dashboard QR code" width="220" height="220">
</div>
```
Wire it (place with the other panel JS; the QR loads lazily on first open, and `/qr.svg` is same-origin so no textContent concern — it's an image element, not server text):
```javascript
document.getElementById("adddev-btn").addEventListener("click", function () {
  var box = document.getElementById("adddev-box");
  var img = document.getElementById("adddev-qr");
  if (box.hidden && !img.src) { img.src = "/qr.svg"; }
  box.hidden = !box.hidden;
});
```

- [ ] **Step 6: Bump PAGE_VERSION + pin the new hash**

In `crr/core/web.py:43` bump to `54` with a comment: `# v54: "Add a device" QR affordance (/qr.svg)`. Run the guard once to get the failure that prints the exact pin line:
```bash
.venv/bin/pytest tests/test_page_version_guard.py -v
```
Append the printed `54: "<sha256>"` entry to `PAGE_PINS` in `tests/test_page_version_guard.py` (never edit an existing entry).

- [ ] **Step 7: Verify new inline JS parses (CI gate)**

The repo syntax-checks served `<script>` blocks with `node --check`. Confirm the new JS passes by running whatever test enforces it (search `tests/` for `node --check`); fix any parse error.

- [ ] **Step 8: Run web tests + guard + full suite + lint**

Run: `.venv/bin/pytest tests/test_web.py tests/test_page_version_guard.py -v && .venv/bin/lint-imports`
Expected: PASS; contract KEPT.

- [ ] **Step 9: Commit**

```bash
git add crr/core/web.py crr/cli.py crr/core/page.html tests/test_web.py tests/test_page_version_guard.py
git commit -m "feat(web): /qr.svg route + 'Add a device' dashboard affordance"
```

---

### Task 6: Print the QR at the end of `bootstrap.sh`

**Files:**
- Modify: `bootstrap.sh` (Summary step)

**Interfaces:**
- Consumes: the installed `crr` binary, `$PORT`, `$TS_DONE`, `$DRY_RUN` (in scope in the Summary step, `bootstrap.sh:569-590`).

**Behavior:** After the dashboard lines and only when not a dry run, invoke `crr qr` so the last thing the user sees post-install is a scannable code. `crr qr` already degrades to a hint if serve isn't live, so no extra guarding is needed beyond dry-run.

- [ ] **Step 1: Add the invocation**

In the Summary step, after the `dashboard:` printf lines (`bootstrap.sh:579`), mirroring the existing dry-run guard used around `crr doctor` (`bootstrap.sh:563-567`):
```sh
  if [ "$DRY_RUN" = 1 ]; then
    note "would print a scannable QR of the dashboard URL (crr qr)"
  else
    printf '\n'
    "$CRR_BIN" qr || true    # informational; never fail the install on it
  fi
```
Use the same variable the script already uses for the installed crr path (it prints a `crr:` line at `:570` — reuse that path variable; if it's not captured in a var, use `crr`).

- [ ] **Step 2: Shell-lint + dry-run smoke**

```bash
bash -n bootstrap.sh          # syntax check
bash bootstrap.sh --dry-run --no-tailscale 2>&1 | tail -20   # see the note line, no exec
```
Expected: `bash -n` clean; dry-run prints the "would print a scannable QR" note and does not exec `crr qr`.

- [ ] **Step 3: Commit**

```bash
git add bootstrap.sh
git commit -m "feat(bootstrap): print the dashboard QR at end of setup"
```

---

## Self-Review

**Spec coverage (Phase 1 rows of the spec):**
- Vendored QR encoder + `to_terminal`/`to_svg` → Task 1. ✓ (segno replaces the hand-rolled `encode`; renderers preserved.)
- `crr/adapters/tailscale.py` `status()`/`serve_status()` tri-state → Task 2. ✓
- URL from `DNSName` not `HostName`; serve-gated → Task 3 (`self_dashboard_url`). ✓
- `crr qr` (terminal) + degrade-with-hint when serve not live → Task 4. ✓
- `/qr.svg` route reusing the same encoder + dashboard affordance → Task 5. ✓
- Printed at end of setup → Task 6. ✓
- Docs: Commands table row → Task 4 Step 5. (The fuller "Get the dashboard on your phone" README section is written in Phase 3, when the launcher + tag setup land, to keep it in one place — noted for the Phase 3 plan.)

**Testing-note reconciliation (spec §Testing):** the spec proposed round-trip decode via a test-only decoder. Because we vendor segno (upstream-proven) rather than hand-roll, correctness is not re-derived here; the wrapper tests assert renderer *shape* and determinism, and the **acceptance check is a manual phone scan** (record it when Phase 1 is verified on lovelace). No pure-Python decoder dependency is added. This is a deliberate, lighter deviation justified by vendoring a trusted encoder — flag it at plan handoff.

**Placeholder scan:** none — every code step carries real code; the two "match the file's exact call style" notes (config loader in Task 4, `handle_request` kwargs in Task 5) are alignment instructions, not deferred logic.

**Type consistency:** `self_dashboard_url(status, serve_status) -> str | None` is produced in Task 3 and consumed identically in Tasks 4 and 5. `to_terminal(text)`/`to_svg(text)` produced in Task 1, consumed in Tasks 4/5. `RealTailscale(timeout).status()/serve_status()` produced in Task 2, consumed in Tasks 4/5. `qr_svg_provider` keyword consistent across web.py and cli.py in Task 5.

**Open integration risks to watch (call out during review, not blockers):**
1. segno's vendored internal imports (Task 1 Step 2) — the smoke test in Step 3 is the gate.
2. segno API names (`terminal(out=…, compact=…)`, `save(kind="svg")`) may vary by version — Task 1 Steps 3/6 say how to adapt.
3. `handle_request`'s exact existing keyword/positional signature — Task 5 Step 1 says to align the test to reality before implementing.
