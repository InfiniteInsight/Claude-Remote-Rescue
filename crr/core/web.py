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

# Discipline: bump this whenever crr/core/page.html changes after a release,
# or clients holding a cached page never learn to reload (see CONTRIBUTING.md).
PAGE_VERSION = 3  # v3: certain vs uncertain (guessed-sid) duplicate weighting
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


ACTIONS = ("reopen", "dismiss", "remove")


def _plain(status: int, text: str) -> Response:
    return _resp(status, "text/plain; charset=utf-8", text.encode("utf-8"))


def _json(status: int, obj: Any) -> Response:
    return _resp(status, "application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def handle_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes = b"",
    *,
    sessions_provider: Callable[[], dict[str, Any]],
    action_provider: Callable[[str, int], tuple[bool, str]] | None = None,
    diagnostics_provider: Callable[[], dict[str, Any]] | None = None,
    allowed_hosts: set[str],
    allowed_suffixes: tuple[str, ...],
    page_version: int = PAGE_VERSION,
) -> Response:
    # Host allowlist first — before any routing or work (DNS-rebinding defense).
    if not host_allowed(_header(headers, "Host"), allowed_hosts, allowed_suffixes):
        return _plain(403, "forbidden")

    if method == "GET":
        if path == "/":
            return _resp(200, "text/html; charset=utf-8", render_page(page_version).encode("utf-8"))
        if path == "/api/sessions":
            return _json(200, sessions_provider())
        if path == "/api/version":
            return _json(200, {"version": page_version})
        if path == "/api/diagnostics":
            # Lazy: computed only when the panel is opened, never on the poll.
            if diagnostics_provider is None:
                return _plain(404, "not found")
            return _json(200, diagnostics_provider())
        return _plain(404, "not found")

    if method == "POST":
        if path != "/api/action":
            return _plain(404, "not found")
        # Require a JSON body: the forced CORS preflight (plus zero CORS
        # headers) is what kills simple-request CSRF. Match the media type
        # tolerantly (clients append "; charset=utf-8").
        ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return _plain(415, "content-type must be application/json")
        try:
            data = json.loads(body or b"")
        except (ValueError, TypeError):
            return _plain(400, "invalid JSON")
        op = data.get("op") if isinstance(data, dict) else None
        pid = data.get("pid") if isinstance(data, dict) else None
        # Strict validation: known op, and a real positive int pid (bool is
        # an int subclass — reject it).
        if op not in ACTIONS or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return _plain(400, "invalid op or pid")
        if action_provider is None:
            return _plain(503, "actions unavailable")
        ok, message = action_provider(op, pid)
        return _json(200 if ok else 409, {"ok": ok, "message": message})

    return _plain(405, "method not allowed")
