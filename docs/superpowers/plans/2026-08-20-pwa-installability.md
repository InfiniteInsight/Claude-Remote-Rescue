# PWA Installability (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the crr dashboard installable as a Progressive Web App so users get a permanent home-screen icon ("Add to Home Screen") — solving the repeat-access problem from Phase 1's QR bridge.

**Architecture:** Five new GET routes (`/manifest.webmanifest`, `/sw.js`, `/icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png`) served from `web.py`, backed by a new pure-core `crr/core/pwa.py` that generates all assets using stdlib only (`struct` + `zlib` for PNG, `json` for the manifest, a string constant for the SW). The `page.html` gets manifest/apple-touch links, iOS meta tags, and a SW registration snippet. HTTPS is already provided by `tailscale serve`.

**Tech Stack:** Python stdlib only (`struct`, `zlib`, `json`). No runtime deps.

## Global Constraints

- **Zero runtime dependencies.** PNG icons generated via `struct` + `zlib` (stdlib). No Pillow, no committed binaries.
- **One-way layering** (`.importlinter`-enforced): `cli → adapters → core`. `crr.core` imports neither adapters nor cli. All new code lives in `crr/core/`.
- **`Cache-Control: no-store`** on every response (enforced by `_resp()`). The service worker MUST NOT cache HTML or `/api/*` — the PAGE_VERSION self-heal depends on the browser always fetching fresh HTML.
- **PAGE_VERSION bump** on every `page.html` change (enforced by `test_page_version_guard.py`): 55 → 56. Append new pin. Update `test_page_version_is_N`.
- **Test-first.** No test touches real tailscale, installs a real PWA, opens a browser, or hits the network.
- **SW syntax check.** The SW source is served at `/sw.js` (not a `<script>` block), so it escapes the `node --check` CI gate. An explicit syntax-validity test is required.
- **Content-Type exactness.** SW must be `text/javascript`. Manifest must be `application/manifest+json`. Icons must be `image/png`. Wrong MIME types silently break browser registration.
- **Acceptance gate (manual, not automated).** After all tasks pass: install the PWA on a real phone over the HTTPS tailnet URL. Verify (a) install prompt appears, (b) home-screen icon works, (c) PAGE_VERSION self-heal still works with the SW controlling the page. Green unit tests do not equal a working PWA.

---

### Task 1: PWA asset generator — `crr/core/pwa.py`

**Files:**
- Create: `crr/core/pwa.py`
- Create: `tests/test_pwa.py`

**Interfaces:**
- Produces:
  - `make_icon_png(size: int) -> bytes` — returns a valid PNG icon of the given dimensions
  - `manifest_json() -> str` — returns the web app manifest as a JSON string
  - `SERVICE_WORKER_JS: str` — the service worker JavaScript source code

- [ ] **Step 1: Write the failing tests for `make_icon_png`**

Create `tests/test_pwa.py`:

```python
"""PWA asset generator tests — icons, manifest, service worker."""

import json
import struct
import zlib

import pytest

from crr.core.pwa import SERVICE_WORKER_JS, make_icon_png, manifest_json


class TestMakeIconPng:
    @pytest.mark.parametrize("size", [180, 192, 512])
    def test_valid_png_header(self, size: int):
        data = make_icon_png(size)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.parametrize("size", [180, 192, 512])
    def test_ihdr_dimensions(self, size: int):
        data = make_icon_png(size)
        # IHDR is the first chunk after the 8-byte signature.
        # 4 bytes length + 4 bytes "IHDR" + 4 bytes width + 4 bytes height
        w, h = struct.unpack(">II", data[16:24])
        assert w == size
        assert h == size

    @pytest.mark.parametrize("size", [180, 192, 512])
    def test_idat_decompresses_to_correct_length(self, size: int):
        """Verify the IDAT payload is a valid zlib stream with the right
        number of bytes for an RGB image: height * (1 filter-byte + width * 3)."""
        data = make_icon_png(size)
        # Walk chunks to find IDAT
        pos = 8  # after PNG signature
        idat_data = b""
        while pos < len(data):
            chunk_len = struct.unpack(">I", data[pos : pos + 4])[0]
            chunk_type = data[pos + 4 : pos + 8]
            if chunk_type == b"IDAT":
                idat_data += data[pos + 8 : pos + 8 + chunk_len]
            pos += 12 + chunk_len  # length + type + data + crc
        raw = zlib.decompress(idat_data)
        expected = size * (1 + size * 3)  # RGB, filter byte per row
        assert len(raw) == expected

    @pytest.mark.parametrize("size", [180, 192, 512])
    def test_filter_bytes_are_zero(self, size: int):
        """Every row's filter byte must be 0 (None filter)."""
        data = make_icon_png(size)
        pos = 8
        idat_data = b""
        while pos < len(data):
            chunk_len = struct.unpack(">I", data[pos : pos + 4])[0]
            chunk_type = data[pos + 4 : pos + 8]
            if chunk_type == b"IDAT":
                idat_data += data[pos + 8 : pos + 8 + chunk_len]
            pos += 12 + chunk_len
        raw = zlib.decompress(idat_data)
        row_stride = 1 + size * 3
        for row in range(size):
            assert raw[row * row_stride] == 0, f"row {row} filter byte != 0"

    def test_deterministic(self):
        """Same size always produces identical bytes."""
        assert make_icon_png(192) == make_icon_png(192)

    def test_cached(self):
        """Second call returns the same object (cached, not regenerated)."""
        a = make_icon_png(192)
        b = make_icon_png(192)
        assert a is b
```

- [ ] **Step 2: Write the failing tests for `manifest_json`**

Append to `tests/test_pwa.py`:

```python
class TestManifestJson:
    def test_valid_json(self):
        obj = json.loads(manifest_json())
        assert isinstance(obj, dict)

    def test_required_keys(self):
        obj = json.loads(manifest_json())
        assert obj["name"] == "Claude-Remote-Rescue"
        assert obj["short_name"] == "CRR"
        assert obj["start_url"] == "/"
        assert obj["display"] == "standalone"

    def test_icons_reference_correct_sizes(self):
        obj = json.loads(manifest_json())
        icons = obj["icons"]
        sizes = {i["sizes"] for i in icons}
        assert "192x192" in sizes
        assert "512x512" in sizes
        for icon in icons:
            assert icon["type"] == "image/png"
            assert icon["src"].startswith("/icon-")

    def test_theme_and_background_color(self):
        obj = json.loads(manifest_json())
        assert "theme_color" in obj
        assert "background_color" in obj
```

- [ ] **Step 3: Write the failing tests for `SERVICE_WORKER_JS`**

Append to `tests/test_pwa.py`:

```python
class TestServiceWorkerJs:
    def test_no_cache_storage_apis(self):
        """Guard: the SW must never use CacheStorage — it would defeat
        the PAGE_VERSION self-heal (Cache-Control: no-store on everything)."""
        forbidden = ["caches.open", "caches.match", "cache.put",
                      "cache.add", "cache.addAll", "CacheStorage"]
        for api in forbidden:
            assert api not in SERVICE_WORKER_JS, (
                f"SERVICE_WORKER_JS contains '{api}' — this would defeat "
                "the PAGE_VERSION self-heal mechanism"
            )

    def test_has_fetch_listener(self):
        assert "addEventListener" in SERVICE_WORKER_JS
        assert "'fetch'" in SERVICE_WORKER_JS or '"fetch"' in SERVICE_WORKER_JS

    def test_syntax_valid(self):
        """The SW is served at /sw.js, not in a <script> block, so it
        escapes the node --check CI gate. Verify syntax explicitly."""
        import subprocess
        result = subprocess.run(
            ["node", "--check", "--input-type=module"],
            input=SERVICE_WORKER_JS,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"SW syntax error: {result.stderr}"
```

- [ ] **Step 4: Run tests to verify they all fail**

Run: `python -m pytest tests/test_pwa.py -v`
Expected: ImportError — `crr.core.pwa` does not exist yet.

- [ ] **Step 5: Implement `crr/core/pwa.py`**

Create `crr/core/pwa.py`:

```python
"""PWA assets — icons, manifest, service worker source.

All functions are pure (no I/O). Icons are generated from stdlib only
(struct + zlib) to maintain the zero-runtime-deps constraint.
"""

from __future__ import annotations

import json
import struct
import zlib

# -- Icon generation --------------------------------------------------------

_BG = (0x0F, 0x11, 0x15)   # dashboard background (#0f1115)
_FG = (0x46, 0xC2, 0x6A)   # live-indicator green  (#46c26a)

_ICON_CACHE: dict[int, bytes] = {}


def make_icon_png(size: int) -> bytes:
    """Generate a branded PNG icon: green circle on dark background.

    Uses struct + zlib only (stdlib). Cached per size.
    """
    if size in _ICON_CACHE:
        return _ICON_CACHE[size]
    cx = cy = size // 2
    radius_sq = (size * 2 // 5) ** 2
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")  # PNG filter: None
        for x in range(size):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius_sq:
                row.extend(_FG)
            else:
                row.extend(_BG)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    _ICON_CACHE[size] = png
    return png


# -- Manifest ---------------------------------------------------------------

def manifest_json() -> str:
    """Return the web app manifest as a JSON string."""
    return json.dumps(
        {
            "name": "Claude-Remote-Rescue",
            "short_name": "CRR",
            "start_url": "/",
            "display": "standalone",
            "theme_color": "#0f1115",
            "background_color": "#0f1115",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        },
        ensure_ascii=False,
    )


# -- Service Worker ----------------------------------------------------------

SERVICE_WORKER_JS = """\
self.addEventListener('fetch', function(event) {
  event.respondWith(fetch(event.request));
});
"""
```

- [ ] **Step 6: Run tests to verify they all pass**

Run: `python -m pytest tests/test_pwa.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Run full test suite**

Run: `python -m pytest --tb=short -q`
Expected: All pass, no regressions.

- [ ] **Step 8: Commit**

```bash
git add crr/core/pwa.py tests/test_pwa.py
git commit -m "feat(pwa): add PWA asset generator — icons, manifest, service worker

Pure core module generating branded PNG icons (struct+zlib), a web app
manifest, and a network-pass-through service worker. Zero runtime deps.
SW is guarded against CacheStorage usage (PAGE_VERSION self-heal)."
```

---

### Task 2: Web routes — manifest, service worker, icons

**Files:**
- Modify: `crr/core/web.py` (lines 349–363, insert new routes before the 404 fallback at line 363)
- Modify: `tests/test_web.py` (append new test class)

**Interfaces:**
- Consumes:
  - `crr.core.pwa.make_icon_png(size: int) -> bytes`
  - `crr.core.pwa.manifest_json() -> str`
  - `crr.core.pwa.SERVICE_WORKER_JS: str`
- Produces: Five new GET routes accessible via `handle_request`

- [ ] **Step 1: Write the failing tests for the five routes**

Append to `tests/test_web.py` (a new class, following the existing pattern of `_get()` helper calls):

```python
class TestPwaRoutes:
    """Manifest, service worker, and icon routes for PWA installability."""

    def test_manifest_route(self):
        resp = _get("/manifest.webmanifest")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/manifest+json"
        obj = json.loads(resp.body)
        assert obj["name"] == "Claude-Remote-Rescue"
        assert obj["display"] == "standalone"

    def test_sw_route(self):
        resp = _get("/sw.js")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/javascript"
        assert b"fetch" in resp.body

    def test_icon_192_route(self):
        resp = _get("/icon-192.png")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_icon_512_route(self):
        resp = _get("/icon-512.png")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_apple_touch_icon_route(self):
        resp = _get("/apple-touch-icon.png")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web.py::TestPwaRoutes -v`
Expected: FAIL — routes return 404 (the fallback at web.py:363).

- [ ] **Step 3: Add the five routes to `web.py`**

In `crr/core/web.py`, add the import at the top (after the existing `from crr.core import contracts` line):

```python
from crr.core import pwa
```

Then insert the five routes BEFORE the `return _plain(404, "not found")` fallback on line 363 (after the `/qr.svg` block that ends at line 362):

```python
        if path == "/manifest.webmanifest":
            return _resp(200, "application/manifest+json",
                         pwa.manifest_json().encode("utf-8"))
        if path == "/sw.js":
            return _resp(200, "text/javascript",
                         pwa.SERVICE_WORKER_JS.encode("utf-8"))
        if path == "/icon-192.png":
            return _resp(200, "image/png", pwa.make_icon_png(192))
        if path == "/icon-512.png":
            return _resp(200, "image/png", pwa.make_icon_png(512))
        if path == "/apple-touch-icon.png":
            return _resp(200, "image/png", pwa.make_icon_png(180))
```

- [ ] **Step 4: Run route tests to verify they pass**

Run: `python -m pytest tests/test_web.py::TestPwaRoutes -v`
Expected: All PASS.

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest --tb=short -q`
Expected: All pass, no regressions.

- [ ] **Step 6: Verify import linter**

Run: `python -m importlinter`
Expected: All contracts satisfied. (`crr.core.pwa` importing only stdlib — no layering violations.)

- [ ] **Step 7: Commit**

```bash
git add crr/core/web.py tests/test_web.py
git commit -m "feat(pwa): add manifest, SW, and icon routes

Five new GET routes in web.py backed by crr.core.pwa:
- /manifest.webmanifest (application/manifest+json)
- /sw.js (text/javascript, network-pass-through)
- /icon-192.png, /icon-512.png, /apple-touch-icon.png (image/png)"
```

---

### Task 3: `page.html` PWA integration + PAGE_VERSION bump

**Files:**
- Modify: `crr/core/page.html` (lines 1–6, add manifest/icon links and iOS meta tags to `<head>`)
- Modify: `crr/core/web.py` (line 43, bump `PAGE_VERSION` 55 → 56)
- Modify: `tests/test_page_version_guard.py` (line 38, append v56 pin)
- Modify: `tests/test_web.py` (line 930, rename `test_page_version_is_55` → `test_page_version_is_56`)

**Interfaces:**
- Consumes: Routes from Task 2 (the manifest and icon URLs referenced in the HTML)
- Produces: An installable PWA when served over HTTPS

- [ ] **Step 1: Add PWA tags to `page.html` `<head>`**

In `crr/core/page.html`, after line 6 (`<title>Claude-Remote-Rescue</title>`), add:

```html
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="CRR">
<meta name="theme-color" content="#0f1115">
```

- [ ] **Step 2: Add service worker registration to `page.html`**

Find the opening `<script>` tag in `page.html` (after the `</style>` closing tag). At the very top of the script body, add:

```javascript
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js');}
```

This is one line, non-blocking. If SW isn't supported, it silently skips.

- [ ] **Step 3: Run `node --check` on the updated script**

Run: `python -c "from crr.core.web import extract_scripts, render_page; [__import__('subprocess').run(['node','--check','--input-type=module'],input=s,text=True,check=True) for s in extract_scripts(render_page(55))]"`
Expected: No errors (the SW registration line is valid JS).

- [ ] **Step 4: Bump PAGE_VERSION 55 → 56**

In `crr/core/web.py`, change line 43:

```python
PAGE_VERSION = 56  # v56: PWA installability — manifest, icons, SW registration
```

- [ ] **Step 5: Compute the new page hash and append the pin**

Run: `python -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('crr/core/page.html').read_bytes()).hexdigest())"`

In `tests/test_page_version_guard.py`, insert a new entry at the top of `PAGE_PINS` (line 38):

```python
PAGE_PINS: dict[int, str] = {
    56: "<computed-sha256-here>",
    55: "48a41cc60dd416d36fd6e3463b42f1ea4eb70d53d80937cd5d07e208f21f4367",
    ...
}
```

- [ ] **Step 6: Update the version test in `test_web.py`**

In `tests/test_web.py`, rename the function at line 930:

```python
def test_page_version_is_56():
    assert web.PAGE_VERSION == 56
```

- [ ] **Step 7: Run the page-version guard tests**

Run: `python -m pytest tests/test_page_version_guard.py -v`
Expected: All PASS — pin matches, no duplicates.

- [ ] **Step 8: Run the full test suite**

Run: `python -m pytest --tb=short -q`
Expected: All pass, no regressions.

- [ ] **Step 9: Verify import linter**

Run: `python -m importlinter`
Expected: All contracts satisfied.

- [ ] **Step 10: Commit**

```bash
git add crr/core/page.html crr/core/web.py tests/test_page_version_guard.py tests/test_web.py
git commit -m "feat(pwa): page.html PWA integration — manifest, icons, SW, iOS meta

Adds <link rel=manifest>, <link rel=apple-touch-icon>, iOS meta tags,
and service worker registration. PAGE_VERSION 55 → 56."
```

---

## Deferred items (tracked locally, not automated)

Two additional requirements identified during Phase 1 that are separate from this PWA work:

1. **Dashboard reauth for expired Claude Code login** — when Claude Code's login expires, the phone/dashboard needs a way to trigger reauthentication. Needs its own brainstorm → spec cycle.

2. **tmux/tab reopen reliability** — audit `crr reopen` code paths for scenarios where it reports success but no tmux window or terminal tab is actually visible. Investigate Windows Terminal `wt.exe` tab spawn failures.
