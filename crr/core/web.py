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
from urllib.parse import parse_qs

from crr.core import config as cfg
from crr.core import contracts

# Bump this whenever crr/core/page.html changes: the served page compares it
# to /api/version every `version_check_seconds` and reloads itself when they
# differ, so reusing a number strands every open dashboard on stale
# JavaScript (see CONTRIBUTING.md).
#
# This is ENFORCED, not just documented (#59): tests/test_page_version_guard.py
# pins a content hash of page.html against this number and fails if the page
# moves without it. Two branches also collided on this number twice in two
# days; git caught both because it is one line, but a page change that simply
# forgets to bump merges clean, which is what the guard is for.
PAGE_VERSION = 45  # v45: the parked state renders as "restored" (Phase 0)
_VERSION_PLACEHOLDER = "@PAGE_VERSION@"
_POLL_PLACEHOLDER = "@POLL_MS@"
_VERSION_MS_PLACEHOLDER = "@VERSION_MS@"
_CONFIRM_ARM_MS_PLACEHOLDER = "@CONFIRM_ARM_MS@"
_NOTICE_MS_PLACEHOLDER = "@NOTICE_MS@"
_RELOAD_DELAY_MS_PLACEHOLDER = "@RELOAD_DELAY_MS@"
_DIAG_ERR_CAP_PLACEHOLDER = "@DIAG_ERR_CAP@"
_FLASH_MS_PLACEHOLDER = "@FLASH_MS@"
_FILTER_DEBOUNCE_MS_PLACEHOLDER = "@FILTER_DEBOUNCE_MS@"
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


# Template snapshot ([lesson 2026-08-01: template/code skew]): a long-running
# service used to re-read page.html from disk on every request, so a branch
# checkout under the running service served a template whose placeholders the
# loaded code could not substitute — one raw @PLACEHOLDER@ is a JS syntax
# error and the dashboard renders nothing. The template is now read once and
# pinned for the process lifetime; the composition root warms it at service
# startup, so a running service always serves the code+template pair it
# started with and a restart is the deliberate deploy step.
_PAGE_CACHE: str | None = None


def _read_page_from_disk() -> str:
    return resources.files("crr.core").joinpath("page.html").read_text(encoding="utf-8")


def load_page() -> str:
    global _PAGE_CACHE
    if _PAGE_CACHE is None:
        _PAGE_CACHE = _read_page_from_disk()
    return _PAGE_CACHE


def render_page(
    version: int = PAGE_VERSION,
    *,
    poll_seconds: int | None = None,
    version_check_seconds: int | None = None,
    confirm_arm_seconds: int | None = None,
    notice_seconds: int | None = None,
    reload_delay_ms: int | None = None,
    diag_error_display_cap: int | None = None,
    flash_ms: int | None = None,
    filter_debounce_ms: int | None = None,
) -> str:
    """Serve-time substitution of version + configured intervals into the page."""
    poll = cfg.DEFAULTS["dashboard_poll_seconds"] if poll_seconds is None else poll_seconds
    vchk = cfg.DEFAULTS["version_check_seconds"] if version_check_seconds is None else version_check_seconds
    confirm_arm = (
        cfg.DEFAULTS["confirm_arm_seconds"] if confirm_arm_seconds is None else confirm_arm_seconds
    )
    notice = cfg.DEFAULTS["notice_seconds"] if notice_seconds is None else notice_seconds
    reload_delay = (
        cfg.DEFAULTS["reload_delay_ms"] if reload_delay_ms is None else reload_delay_ms
    )
    diag_err_cap = (
        cfg.DEFAULTS["diag_error_display_cap"]
        if diag_error_display_cap is None
        else diag_error_display_cap
    )
    flash = cfg.DEFAULTS["flash_ms"] if flash_ms is None else flash_ms
    debounce = (
        cfg.DEFAULTS["filter_debounce_ms"] if filter_debounce_ms is None else filter_debounce_ms
    )
    return (
        load_page()
        .replace(_VERSION_PLACEHOLDER, str(version))
        .replace(_POLL_PLACEHOLDER, str(int(poll) * 1000))
        .replace(_VERSION_MS_PLACEHOLDER, str(int(vchk) * 1000))
        .replace(_CONFIRM_ARM_MS_PLACEHOLDER, str(int(confirm_arm) * 1000))
        .replace(_NOTICE_MS_PLACEHOLDER, str(int(notice) * 1000))
        .replace(_RELOAD_DELAY_MS_PLACEHOLDER, str(int(reload_delay)))
        .replace(_DIAG_ERR_CAP_PLACEHOLDER, str(int(diag_err_cap)))
        .replace(_FLASH_MS_PLACEHOLDER, str(int(flash)))
        .replace(_FILTER_DEBOUNCE_MS_PLACEHOLDER, str(int(debounce)))
    )


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


ACTIONS = ("reopen", "dismiss", "remove", "kick", "close", "untrack", "detmux", "untmux")

# Sid-keyed actions — a SEPARATE namespace/endpoint from the pid-keyed
# ACTIONS above (see POST /api/sid-action). "retrack" is C2; "adopt" (C3)
# journals a transcript crr never tracked (T-C discovery); "takeover" stops
# a still-live claude for that sid, then adopts (non-blocking on the web —
# see cli._web_takeover), giving `crr adopt --takeover` a dashboard button.
# "autokick-on"/"autokick-off" (spec 2026-08-07, Slice 3) pin ONE session's
# auto-kick opt-in/opt-out, keyed by sid (never pid — see settings.py's
# module docstring for why). Two explicit ops rather than one op carrying a
# bool value, matching every other op here: the shape stays "op + sid",
# nothing more to validate.
SID_ACTIONS = ("retrack", "adopt", "takeover", "autokick-on", "autokick-off")


# Rows per page in the dashboard's discoverable modal (see crr.core.config's
# `discoverable_page_size` — that DEFAULTS entry is the injectable prior;
# this constant only supplies the fallback for the query-param parse below).
DISCOVERABLE_PAGE = cfg.DEFAULTS["discoverable_page_size"]


def _positive_int(raw: str, default: int) -> int:
    """Parse a non-negative int from a query param, falling back on junk.

    A browse surface must not 400 because a hand-edited URL had
    ``offset=-5`` or ``limit=abc``; it degrades to the default instead.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


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
    action_provider: Callable[[str, int], tuple[bool, str, bool]] | None = None,
    diagnostics_provider: Callable[[], dict[str, Any]] | None = None,
    untracked_provider: Callable[[str, int, int], dict[str, Any]] | None = None,
    discoverable_provider: Callable[[str, int, int], dict[str, Any]] | None = None,
    sid_action_provider: Callable[[str, str], tuple[bool, str, bool]] | None = None,
    recall_provider: Callable[[str, str | None], dict] | None = None,
    exclusions_provider: Callable[[], dict[str, Any]] | None = None,
    exclusions_writer: Callable[[Any], dict[str, Any]] | None = None,
    settings_provider: Callable[[], dict[str, Any]] | None = None,
    settings_writer: Callable[[Any], dict[str, Any]] | None = None,
    allowed_hosts: set[str],
    allowed_suffixes: tuple[str, ...],
    query: str = "",
    page_version: int = PAGE_VERSION,
    poll_seconds: int | None = None,
    version_check_seconds: int | None = None,
    confirm_arm_seconds: int | None = None,
    notice_seconds: int | None = None,
    reload_delay_ms: int | None = None,
    diag_error_display_cap: int | None = None,
    flash_ms: int | None = None,
    filter_debounce_ms: int | None = None,
) -> Response:
    # Host allowlist first — before any routing or work (DNS-rebinding defense).
    if not host_allowed(_header(headers, "Host"), allowed_hosts, allowed_suffixes):
        return _plain(403, "forbidden")

    if method == "GET":
        if path == "/":
            page = render_page(
                page_version, poll_seconds=poll_seconds, version_check_seconds=version_check_seconds,
                confirm_arm_seconds=confirm_arm_seconds, notice_seconds=notice_seconds,
                reload_delay_ms=reload_delay_ms, diag_error_display_cap=diag_error_display_cap,
                flash_ms=flash_ms, filter_debounce_ms=filter_debounce_ms,
            )
            return _resp(200, "text/html; charset=utf-8", page.encode("utf-8"))
        if path == "/api/sessions":
            return _json(200, sessions_provider())
        if path == "/api/version":
            return _json(200, {"version": page_version})
        if path == "/api/diagnostics":
            # Lazy: computed only when the panel is opened, never on the poll.
            if diagnostics_provider is None:
                return _plain(404, "not found")
            return _json(200, diagnostics_provider())
        if path == "/api/untracked":
            # Lazy, like diagnostics: computed only when the panel is
            # opened, never on the poll path.
            if untracked_provider is None:
                return _plain(404, "not found")
            # Paged + filtered exactly like /api/discoverable (same modal, so
            # the same contract) — see that branch for the param handling.
            params = parse_qs(query)
            unt_q = (params.get("q", [""])[0]).strip()
            offset = _positive_int(params.get("offset", [""])[0], 0)
            limit = _positive_int(params.get("limit", [""])[0], DISCOVERABLE_PAGE) or DISCOVERABLE_PAGE
            return _json(200, untracked_provider(unt_q, offset, limit))
        if path == "/api/discoverable":
            # Lazy (T-C): the untracked-transcript scan reads transcript
            # content per candidate — never on the poll path, only when the
            # dashboard's "Discoverable" panel is opened. Shape:
            # [{session_id, sid8, cwd, last_active, transcript_bytes,
            #   last_prompt}, ...] — see crr.core.discovery.untracked.
            if discoverable_provider is None:
                return _plain(404, "not found")
            # Paged + filtered server-side: enriching every untracked
            # transcript to render one page cost ~10s on a machine with a few
            # thousand of them. Junk/absent paging params fall back to the
            # defaults rather than erroring — this is a browse surface, and a
            # 400 here would just blank the panel.
            params = parse_qs(query)
            disc_q = (params.get("q", [""])[0]).strip()
            offset = _positive_int(params.get("offset", [""])[0], 0)
            limit = _positive_int(params.get("limit", [""])[0], DISCOVERABLE_PAGE) or DISCOVERABLE_PAGE
            return _json(200, discoverable_provider(disc_q, offset, limit))
        if path == "/api/recall":
            # Lazy GET (never the poll path): transcript search. The query
            # string is parsed here (the composition root passes it in as
            # `query`, keeping this handler a pure function). Guards mirror
            # `crr recall`: a missing/empty/whitespace `q` is rejected (an
            # empty substring matches every turn), and `sid` — when given —
            # must be a real UUID before it reaches find_transcript's glob.
            if recall_provider is None:
                return _plain(404, "not found")
            params = parse_qs(query)
            q = (params.get("q", [""])[0]).strip()
            if not q:
                return _plain(400, "recall: query (q) required")
            sid_vals = params.get("sid")
            sid = sid_vals[0] if sid_vals else None
            if sid is not None and not contracts.valid_session_id(sid):
                return _plain(400, "recall: invalid session id")
            return _json(200, recall_provider(q, sid))
        if path == "/api/exclusions":
            # Admin read: which directories discovery skips, split by owner
            # (config.toml baseline vs dashboard-managed) so the UI can show
            # the user which ones it is allowed to edit.
            if exclusions_provider is None:
                return _plain(404, "not found")
            return _json(200, exclusions_provider())
        if path == "/api/settings":
            # The Settings modal's global auto-kick row (spec 2026-08-07,
            # Slice 3): the dashboard's stored override (nullable — None
            # means "unset, fall back to config"), the resolved effective
            # value, config.toml's own default, and whether the settings
            # file itself is unreadable — that last one matters because a
            # degraded file means the watchdog auto-kicks NOTHING regardless
            # of what this toggle shows (fail-closed, Slice 2), and the user
            # needs to see why from the phone. Shape is the provider's call;
            # see cli._cmd_web's settings_provider for the concrete keys.
            if settings_provider is None:
                return _plain(404, "not found")
            return _json(200, settings_provider())
        return _plain(404, "not found")

    if method == "POST":
        if path == "/api/action":
            # Require a JSON body: the forced CORS preflight (plus zero CORS
            # headers) is what kills simple-request CSRF. Match the media
            # type tolerantly (clients append "; charset=utf-8").
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            op = data.get("op") if isinstance(data, dict) else None
            pid = data.get("pid") if isinstance(data, dict) else None
            # Strict validation: known op, and a real positive int pid (bool
            # is an int subclass — reject it). Left exactly as-is (C2): the
            # sid-keyed endpoint below is deliberately separate rather than
            # loosening this gate.
            if op not in ACTIONS or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return _plain(400, "invalid op or pid")
            if action_provider is None:
                return _plain(503, "actions unavailable")
            ok, message, degraded = action_provider(op, pid)
            # degraded is NOT an error status: the op succeeded, so the fetch
            # guards in page.html must stay on the success path. Only the
            # notice styling changes ([user request, 2026-08-09]).
            return _json(200 if ok else 409,
                         {"ok": ok, "message": message, "degraded": degraded})

        if path == "/api/exclusions":
            # Same CSRF posture as /api/action (host allowlist already ran;
            # JSON content-type gate; no CORS headers are ever emitted).
            # This one WRITES TO DISK, so the gate matters at least as much;
            # the writer owns validation/bounds and signals a bad list by
            # raising ValueError, which becomes a 400 with its message.
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            if not isinstance(data, dict) or not isinstance(data.get("dirs"), list):
                return _plain(400, "expected {\"dirs\": [...]}")
            if exclusions_writer is None:
                return _plain(503, "exclusions unavailable")
            try:
                return _json(200, exclusions_writer(data["dirs"]))
            except ValueError as exc:
                return _plain(400, str(exc))

        if path == "/api/settings":
            # Same CSRF posture as /api/exclusions (host allowlist already
            # ran; JSON content-type gate; no CORS headers are ever
            # emitted) — this one writes to disk too. The writer
            # (SettingsStore.write_global_autokick) owns the bool-or-None
            # validation and signals a rejection by raising ValueError,
            # which becomes a 400 carrying its message — same contract as
            # exclusions_writer.
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            if not isinstance(data, dict) or "autokick" not in data:
                return _plain(400, 'expected {"autokick": true|false|null}')
            if settings_writer is None:
                return _plain(503, "settings unavailable")
            try:
                return _json(200, settings_writer(data["autokick"]))
            except ValueError as exc:
                return _plain(400, str(exc))

        if path == "/api/sid-action":
            # Same CSRF posture as /api/action (JSON content-type gate; the
            # host allowlist already ran above; no CORS headers are ever
            # emitted) — a separate route + validator so a sid-keyed op can
            # never be reached through the strict pid-keyed gate above.
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            op = data.get("op") if isinstance(data, dict) else None
            sid = data.get("sid") if isinstance(data, dict) else None
            if op not in SID_ACTIONS or not contracts.valid_session_id(sid):
                return _plain(400, "invalid op or sid")
            if sid_action_provider is None:
                return _plain(503, "actions unavailable")
            ok, message, degraded = sid_action_provider(op, sid)
            return _json(200 if ok else 409,
                         {"ok": ok, "message": message, "degraded": degraded})

        return _plain(404, "not found")

    return _plain(405, "method not allowed")
