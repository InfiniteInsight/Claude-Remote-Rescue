"""Web dashboard request handler — pure core, no http.server coupling.

``handle_request`` is a pure function of (method, path, headers) plus a
``sessions_provider`` callback, so every branch — especially the security
gate — is unit-testable with fake requests. The cli layer owns the actual
socket (bound to loopback) and wires the real journal/adapters into the
provider.

Security model (inherited from ccresume):
- Host allowlist as a DNS-rebinding defense: exact match against loopback
  / own hostname / config extras, plus a ``.ts.net`` suffix for tailnet
  names. Port and case are normalized first.
- ``Cache-Control: no-store`` on everything, so the page self-heal
  (PAGE_VERSION vs /api/version) is never defeated by a cached page.
- No CORS headers are ever emitted (that, plus the JSON-Content-Type gate
  on POSTs, is what kills simple-request CSRF — POST handling lands with
  the action endpoints).
- The page renders untrusted fields (last_prompt, cwd) via textContent
  only; see page.html.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any, Callable, Mapping, NamedTuple

PAGE_VERSION = 1
_VERSION_PLACEHOLDER = "@PAGE_VERSION@"
_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)


class Response(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


def _canonical_host(host_header: str) -> str:
    """Lowercase the host and strip any port (handling bracketed IPv6)."""
    host = host_header.strip()
    if not host:
        return ""
    if host.startswith("["):  # [::1] or [::1]:8377
        return host[: host.index("]") + 1].lower() if "]" in host else host.lower()
    if host.count(":") == 1:  # host:port (bare IPv6 has >1 colon, left as-is)
        host = host.rsplit(":", 1)[0]
    return host.lower()


def host_allowed(host_header: str, allowed_hosts: set[str], allowed_suffixes: tuple[str, ...]) -> bool:
    host = _canonical_host(host_header)
    if not host:
        return False
    if host in {h.lower() for h in allowed_hosts}:
        return True
    return any(host.endswith(sfx) for sfx in allowed_suffixes)


def load_page() -> str:
    return resources.files("crr.core").joinpath("page.html").read_text(encoding="utf-8")


def render_page(version: int = PAGE_VERSION) -> str:
    """Serve-time substitution of the single version source into the page."""
    return load_page().replace(_VERSION_PLACEHOLDER, str(version))


def extract_scripts(html: str) -> list[str]:
    """Return the body of every <script> block (for the node --check gate)."""
    return [m.group(1) for m in _SCRIPT_RE.finditer(html)]


def _resp(status: int, content_type: str, body: bytes) -> Response:
    return Response(
        status,
        {"Content-Type": content_type, "Cache-Control": "no-store"},
        body,
    )


def _header(headers: Mapping[str, str], name: str) -> str:
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return ""


def handle_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    *,
    sessions_provider: Callable[[], dict[str, Any]],
    allowed_hosts: set[str],
    allowed_suffixes: tuple[str, ...],
    page_version: int = PAGE_VERSION,
) -> Response:
    # Host allowlist first — before any routing or work.
    if not host_allowed(_header(headers, "Host"), allowed_hosts, allowed_suffixes):
        return _resp(403, "text/plain; charset=utf-8", b"forbidden")

    if method != "GET":
        # POST action endpoints land with the session-op wiring; until then
        # nothing accepts a body.
        return _resp(405, "text/plain; charset=utf-8", b"method not allowed")

    if path == "/":
        return _resp(200, "text/html; charset=utf-8", render_page(page_version).encode("utf-8"))
    if path == "/api/sessions":
        body = json.dumps(sessions_provider(), ensure_ascii=False).encode("utf-8")
        return _resp(200, "application/json", body)
    if path == "/api/version":
        body = json.dumps({"version": page_version}).encode("utf-8")
        return _resp(200, "application/json", body)

    return _resp(404, "text/plain; charset=utf-8", b"not found")
