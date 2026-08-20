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
