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
