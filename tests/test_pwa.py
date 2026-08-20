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
