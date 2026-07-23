"""Web dashboard: stdlib-only HTTP server + single-file page.

The server binds LOOPBACK ONLY (127.0.0.1). It is never exposed to a
network by crr itself: putting it on your tailnet is the user's job (e.g.
``tailscale serve 8377``), and the tailnet -- or the user's own reverse
proxy -- is the entire auth boundary.

Security model (inherited wholesale from ccresume, each rule tested):

- Loopback bind only; no TLS, no auth of its own (see above).
- Host-header allowlist with EXACT-match semantics, checked before any
  routing: {127.0.0.1[:port], localhost[:port], [::1][:port], own
  hostname [:port], any hostname ending in ``.ts.net``, config extras}.
  "evil-localhost" and "localhost.evil.com" are rejected.
- Every POST requires ``Content-Type: application/json`` (else 415).
  Non-simple content types force a CORS preflight, which kills
  simple-request CSRF from a malicious page in the same browser.
- Strict input validation before use: session ids must match the uuid
  regex, pids must match ``^[0-9]{1,10}$``.
- Subprocess use is argv lists only, never shell strings.
- The page builds DOM exclusively via createElement/textContent -- data
  never reaches innerHTML.

[lesson: page self-heal] PAGE_VERSION + /api/version polling +
``Cache-Control: no-store`` on every response: a cached page once shipped
a JS syntax error and the self-heal is what un-bricks clients.

[lesson: snap jq] Status assembly performs at most ONE subprocess spawn
per request (a single batched ``ps -o pid=,tty=`` covering every live
candidate pid); the journal and transcripts are read natively.

[lesson: swallowed exit codes] /api/action returns the structured
OpResult JSON verbatim, with an HTTP status that reflects
success/refusal/failure -- failures propagate, never green checkmarks.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, List, Optional, Tuple

from . import bootid, classify, config, journal, ops, transcript
from .result import (
    EXIT_GAVE_UP,
    EXIT_NOT_FOUND,
    EXIT_NO_TMUX,
    EXIT_REFUSED,
    OpResult,
)

PAGE_VERSION = 1

BIND_HOST = "127.0.0.1"
IDLE_SECONDS = 30 * 60  # updated older than this -> "idle" badge
MAX_BODY_BYTES = 64 * 1024

SID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PID_RE = re.compile(r"^[0-9]{1,10}$")

# action name -> operation. "revive" is the dashboard's alias for
# single-session revival; ops.reopen is classifier-gated.
ACTIONS = {
    "kick": ops.kick,
    "close": ops.close,
    "reopen": ops.reopen,
    "dismiss": ops.dismiss,
    "remove": ops.remove,
    "revive": ops.reopen,
}


# ---------------------------------------------------------------------------
# Host allowlist (exact-match semantics)


def _split_host_header(header: str) -> Tuple[str, Optional[str]]:
    """Split a Host header into (host, port-or-None).

    IPv6 literals keep their brackets ("[::1]"). A malformed header is
    returned whole so it can only match an identical config extra.
    """
    header = header.strip()
    if header.startswith("["):
        end = header.find("]")
        if end == -1:
            return header, None
        host, rest = header[: end + 1], header[end + 1 :]
        if not rest:
            return host, None
        if rest.startswith(":") and rest[1:].isdigit():
            return host, rest[1:]
        return header, None
    if header.count(":") == 1:
        host, port = header.rsplit(":", 1)
        if port.isdigit():
            return host, port
    return header, None


def host_allowed(header: str, port: int, extras: Iterable[str] = ()) -> bool:
    """EXACT-match Host allowlist check.

    Allowed: loopback names, own hostname, *.ts.net hostnames, config
    extras -- each with or without ``:<our port>``. Any other port, and
    any other name ("evil-localhost", "localhost.evil.com"), is rejected.
    """
    if not header:
        return False
    header = header.strip()
    extras = [e.strip() for e in extras if e and e.strip()]
    # A config extra may be listed with an explicit port: match verbatim.
    if header.lower() in {e.lower() for e in extras}:
        return True
    host, hport = _split_host_header(header)
    if hport is not None and hport != str(port):
        return False
    allowed = {"127.0.0.1", "localhost", "[::1]"}
    hostname = socket.gethostname().strip()
    if hostname:
        allowed.add(hostname.lower())
    allowed.update(e.lower() for e in extras)
    host_l = host.lower()
    if host_l in allowed:
        return True
    # Tailnet MagicDNS names: exact domain-suffix match on the hostname.
    if not host_l.startswith("[") and host_l.endswith(".ts.net"):
        return True
    return False


# ---------------------------------------------------------------------------
# Status assembly (poll path: at most one subprocess spawn per request)


def _batch_tty(pids: List[int]) -> Dict[int, str]:
    """One ``ps -o pid=,tty=`` call covering every pid (argv list, never a
    shell string). Missing pids simply do not appear in the result."""
    if not pids:
        return {}
    argv = ["ps", "-o", "pid=,tty=", "-p", ",".join(str(int(p)) for p in pids)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: Dict[int, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[int(parts[0])] = parts[1]
            except ValueError:
                continue
    return out


def _classify_batched(entry: Dict, current_boot: str, ttys: Dict[int, str]) -> str:
    """classify.classify semantics, but the tty check comes from one
    batched ps call instead of a per-entry spawn."""
    entry_boot = entry.get("boot_id") or ""
    if not entry_boot or not current_boot or entry_boot != current_boot:
        return classify.CRASHED
    pid = entry.get("pid")
    if not isinstance(pid, int) or not classify.pid_alive(pid):
        return classify.CRASHED
    tty = ttys.get(pid, "")
    if not tty or tty in ("?", "??", "-"):
        return classify.GHOST
    return classify.LIVE


def _is_idle(updated: Optional[str], now: Optional[datetime] = None) -> bool:
    if not updated:
        return False
    try:
        stamp = datetime.fromisoformat(updated)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - stamp).total_seconds() > IDLE_SECONDS


# Transcript prompt cache: path -> ((mtime_ns, size), prompt). Reads are
# native file IO either way; the cache keeps repeat polls at 25+ sessions
# from re-scanning unchanged transcript tails.
_prompt_cache: Dict[str, Tuple[Tuple[int, int], Optional[str]]] = {}


def _last_msg(sid: Optional[str], cwd: str) -> Optional[str]:
    if not sid or not SID_RE.match(sid):  # validate before any path use
        return None
    path = transcript.transcript_path(sid, cwd)
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    key = str(path)
    hit = _prompt_cache.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    prompt = transcript.last_prompt(sid, cwd)
    _prompt_cache[key] = (sig, prompt)
    return prompt


def assemble_status() -> Dict:
    """Session-card list for /api/status.

    Subprocess budget: exactly one ``ps`` spawn (batched tty lookup),
    regardless of entry count -- well under the <=2-per-entry requirement.
    """
    entries = journal.list_entries()
    current_boot = bootid.current_boot_id()  # native file read on Linux

    candidates = [
        e["pid"]
        for e in entries
        if isinstance(e.get("pid"), int)
        and (e.get("boot_id") or "") == current_boot
        and current_boot
        and classify.pid_alive(e["pid"])
    ]
    ttys = _batch_tty(candidates)

    now = datetime.now(timezone.utc)
    sid_counts: Dict[str, int] = {}
    cards: List[Dict] = []
    for entry in entries:
        claude_info = entry.get("claude") or {}
        sid = claude_info.get("session_id")
        if not (isinstance(sid, str) and SID_RE.match(sid)):
            sid = None
        else:
            sid_counts[sid] = sid_counts.get(sid, 0) + 1
        sid8 = sid[:8] if sid else None
        state = _classify_batched(entry, current_boot, ttys)
        cards.append(
            {
                "pid": entry.get("pid"),
                "sid": sid,
                "sid8": sid8,
                "identity": "#%s · %s" % (entry.get("pid"), sid8 or "—"),
                "sid_verified": bool(claude_info.get("verified", True)) if sid else None,
                "state": state,
                "idle": _is_idle(entry.get("updated"), now),
                "duplicate": False,
                "dup_group": None,
                "cwd": entry.get("cwd"),
                "shell": entry.get("shell"),
                "host": entry.get("host"),
                "last_cmd": entry.get("last_cmd"),
                "last_msg": _last_msg(sid, entry.get("cwd") or ""),
                "tmux_session": entry.get("tmux_session"),
                "updated": entry.get("updated"),
            }
        )

    # Duplicate detection: the same claude sid journaled on more than one
    # entry marks every such card, sharing a group key for tinting.
    for card in cards:
        if card["sid"] and sid_counts.get(card["sid"], 0) > 1:
            card["duplicate"] = True
            card["dup_group"] = card["sid8"]

    counts = {"live": 0, "ghost": 0, "crashed": 0, "idle": 0, "duplicate": 0}
    for card in cards:
        counts[card["state"]] += 1
        if card["idle"]:
            counts["idle"] += 1
        if card["duplicate"]:
            counts["duplicate"] += 1

    return {
        "cards": cards,
        "counts": counts,
        "generated": now.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Diagnostics (lazy: fetched on click, NEVER on the poll path)


def _run_capture(argv: List[str], timeout: float = 5.0) -> Tuple[Optional[str], Optional[str]]:
    """(stdout, None) on success, (None, error) on any failure. argv list
    only, timeout-guarded."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or ("exit status %d" % proc.returncode)
        return None, err
    return proc.stdout, None


def diagnostics() -> Dict:
    """Per-source host diagnostics, each independently timeout-guarded and
    degrading on its own (a dead journald must not blank the others).

    Mirrors the CLI diagnose stub's source names; real platform adapters
    land in later phases.
    """
    sources: Dict[str, Dict] = {}
    if sys.platform.startswith("linux"):
        specs = {
            "boots": ["journalctl", "--list-boots", "--no-pager"],
            "prev_boot_errors": [
                "journalctl", "-b", "-1", "-p", "err", "-n", "50", "--no-pager",
            ],
        }
        for name, argv in specs.items():
            out, err = _run_capture(argv)
            if out is None:
                sources[name] = {"ok": False, "error": err}
            else:
                sources[name] = {"ok": True, "output": out.strip()[-8000:]}
        sources["host_events"] = {"ok": False, "error": "not yet implemented"}
    else:
        for name in ("boots", "prev_boot_errors", "host_events"):
            sources[name] = {
                "ok": False,
                "error": "not yet implemented on this platform",
            }
    return {"sources": sources}


# ---------------------------------------------------------------------------
# HTTP layer


def _http_status_for(res: OpResult) -> int:
    if res.ok:
        return 200
    return {
        EXIT_NOT_FOUND: 404,
        EXIT_REFUSED: 409,
        EXIT_GAVE_UP: 410,
        EXIT_NO_TMUX: 503,
    }.get(res.exit_code, 500)


class Handler(BaseHTTPRequestHandler):
    server_version = "crr"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # quiet by default
        pass

    def _send(self, code: int, obj=None, body: Optional[bytes] = None,
              ctype: str = "application/json") -> None:
        if body is None:
            body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "%s; charset=utf-8" % ctype)
        # [lesson: page self-heal] every response, page and API alike.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        # One response per connection: error paths may leave a request
        # body unread, which must never be parsed as a next request.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _host_ok(self) -> bool:
        """Host allowlist gate: runs before ANY routing."""
        header = self.headers.get("Host", "") or ""
        port = self.server.server_address[1]
        extras = getattr(self.server, "crr_host_extras", ())
        if host_allowed(header, port, extras):
            return True
        self._send(403, {"error": "forbidden", "detail": "Host header not allowed"})
        return False

    # -- GET --------------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, body=PAGE_HTML.encode("utf-8"), ctype="text/html")
        elif path == "/api/status":
            self._send(200, assemble_status())
        elif path == "/api/version":
            self._send(200, {"version": PAGE_VERSION})
        elif path == "/api/diagnostics":
            self._send(200, diagnostics())
        else:
            self._send(404, {"error": "not found"})

    # -- POST -------------------------------------------------------------

    def do_POST(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        if path != "/api/action":
            self._send(404, {"error": "not found"})
            return
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            # Forces a CORS preflight for cross-origin callers: a
            # simple-request CSRF form cannot send application/json.
            self._send(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": "body too large or missing length"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "invalid JSON body"})
            return
        if not isinstance(data, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return

        action = data.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            self._send(400, {"error": "unknown action"})
            return
        pid_raw = data.get("pid")
        if isinstance(pid_raw, bool) or not isinstance(pid_raw, (int, str)):
            self._send(400, {"error": "invalid pid"})
            return
        pid_str = str(pid_raw)
        if not PID_RE.match(pid_str):
            self._send(400, {"error": "invalid pid"})
            return

        try:
            res = ACTIONS[action](int(pid_str))
        except Exception as exc:  # noqa: BLE001 - HTTP boundary
            self._send(
                500,
                {"op": action, "pid": int(pid_str), "ok": False,
                 "status": "internal-error", "detail": str(exc)},
            )
            return
        # [lesson: swallowed exit codes] failure statuses propagate as
        # non-2xx HTTP statuses; the OpResult travels verbatim.
        self._send(_http_status_for(res), res.to_dict())


# ---------------------------------------------------------------------------
# Server lifecycle


def make_server(port: Optional[int] = None, cfg: Optional[Dict] = None) -> ThreadingHTTPServer:
    """Build the loopback-only server. port=0 binds an ephemeral port
    (tests); port=None takes the config value."""
    if cfg is None:
        cfg = config.load_config()
    if port is None:
        port = int(cfg.get("web_port") or config.DEFAULT_WEB_PORT)
    server = ThreadingHTTPServer((BIND_HOST, port), Handler)
    server.daemon_threads = True
    server.crr_host_extras = tuple(cfg.get("host_allowlist") or ())
    return server


def run(port: Optional[int] = None) -> int:
    server = make_server(port=port)
    actual_port = server.server_address[1]
    print("crr web: serving on http://%s:%d/ (loopback only;" % (BIND_HOST, actual_port))
    print("  expose on your tailnet yourself, e.g.: tailscale serve %d)" % actual_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# The page (single file, no external assets; DOM built via
# createElement/textContent only -- data never reaches innerHTML)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crr · Claude Remote Rescue</title>
<style>
:root {
  --bg: #0e1116; --panel: #171c24; --panel2: #1d232d; --line: #2a3240;
  --fg: #dbe2ec; --dim: #8b96a5; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --live: #3ecf8e; --ghost: #e6b450; --crashed: #f0596b; --idle: #7aa2f7; --dup: #c678dd;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px;
  padding: 14px 18px 6px; }
h1 { font-size: 18px; margin: 0; letter-spacing: .5px; }
h1 .sub { color: var(--dim); font-weight: 400; font-size: 13px; margin-left: 8px; }
#counts { display: flex; gap: 8px; flex-wrap: wrap; margin-left: auto; }
.count { background: var(--panel); border: 1px solid var(--line); border-radius: 999px;
  padding: 2px 10px; font-size: 12px; color: var(--dim); }
.count b { color: var(--fg); margin-right: 4px; }
#key { padding: 2px 18px 10px; font-size: 12px; color: var(--dim);
  display: flex; gap: 14px; flex-wrap: wrap; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 5px; vertical-align: 1px; }
.dot.live { background: var(--live); } .dot.ghost { background: var(--ghost); }
.dot.crashed { background: var(--crashed); } .dot.idle { background: var(--idle); }
.dot.dup { background: var(--dup); }
main { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px; padding: 0 18px 24px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; border-left-width: 4px; }
.card.state-live { border-left-color: var(--live); }
.card.state-ghost { border-left-color: var(--ghost); }
.card.state-crashed { border-left-color: var(--crashed); }
.card.dup-0 { background: linear-gradient(0deg, rgba(198,120,221,.08), rgba(198,120,221,.08)), var(--panel); }
.card.dup-1 { background: linear-gradient(0deg, rgba(122,162,247,.08), rgba(122,162,247,.08)), var(--panel); }
.card.dup-2 { background: linear-gradient(0deg, rgba(230,180,80,.08), rgba(230,180,80,.08)), var(--panel); }
.card.dup-3 { background: linear-gradient(0deg, rgba(62,207,142,.08), rgba(62,207,142,.08)), var(--panel); }
.badges { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.badge { font-size: 11px; text-transform: uppercase; letter-spacing: .6px;
  border-radius: 4px; padding: 1px 7px; font-weight: 600; }
.badge.live { background: rgba(62,207,142,.16); color: var(--live); }
.badge.ghost { background: rgba(230,180,80,.16); color: var(--ghost); }
.badge.crashed { background: rgba(240,89,107,.16); color: var(--crashed); }
.badge.idle { background: rgba(122,162,247,.16); color: var(--idle); }
.badge.dup { background: rgba(198,120,221,.16); color: var(--dup); }
.badge.unverified { background: rgba(139,150,165,.16); color: var(--dim); }
.identity { font-family: var(--mono); font-size: 15px; margin-bottom: 4px; }
.cwd { font-family: var(--mono); font-size: 12px; color: var(--dim);
  word-break: break-all; margin-bottom: 6px; }
.meta { font-size: 12px; color: var(--dim); margin-bottom: 6px; }
.lastcmd { font-family: var(--mono); font-size: 12px; color: var(--fg);
  background: var(--panel2); border-radius: 5px; padding: 4px 8px; margin-bottom: 6px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lastmsg { font-size: 12.5px; color: #b7c2d0; font-style: italic; margin-bottom: 8px;
  overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; }
button { background: var(--panel2); color: var(--fg); border: 1px solid var(--line);
  border-radius: 6px; padding: 4px 11px; font-size: 12px; cursor: pointer; }
button:hover { border-color: var(--dim); }
button.danger:hover { border-color: var(--crashed); color: var(--crashed); }
button.diag { margin-left: auto; color: var(--dim); }
pre.diagpanel { background: #0a0d11; border: 1px solid var(--line); border-radius: 6px;
  margin: 8px 0 0; padding: 8px; font-size: 11px; max-height: 260px; overflow: auto;
  white-space: pre-wrap; word-break: break-word; }
#empty { padding: 40px 18px; color: var(--dim); text-align: center; }
#offline { display: none; padding: 6px 18px; color: var(--crashed); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>crr<span class="sub">Claude Remote Rescue</span></h1>
  <div id="counts"></div>
</header>
<div id="key">
  <span><span class="dot live"></span>live — shell up, terminal attached</span>
  <span><span class="dot ghost"></span>ghost — shell orphaned, no terminal</span>
  <span><span class="dot crashed"></span>crashed — pid dead or host rebooted</span>
  <span><span class="dot idle"></span>idle — no journal update in 30&nbsp;min</span>
  <span><span class="dot dup"></span>duplicate — same session id on several entries</span>
</div>
<div id="offline">dashboard unreachable — retrying…</div>
<main id="cards"></main>
<div id="empty" hidden>No sessions journaled.</div>
<script>
"use strict";
var PAGE_VERSION = __PAGE_VERSION__;
var POLL_MS = 5000;
var DESTRUCTIVE = { kick: true, close: true, dismiss: true, remove: true };

function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) { n.className = cls; }
  if (text !== undefined && text !== null) { n.textContent = String(text); }
  return n;
}

function actionsFor(card) {
  var a = [];
  if (card.state === "live") { a.push("kick", "close"); }
  else if (card.state === "ghost") { a.push("close", "dismiss"); }
  else if (card.state === "crashed") {
    if (card.sid) { a.push("reopen"); }
    a.push("dismiss");
  }
  a.push("remove");
  return a;
}

function doAction(action, pid) {
  if (DESTRUCTIVE[action] &&
      !window.confirm(action + " session #" + pid + "?")) { return; }
  fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: action, pid: pid })
  }).then(function (resp) {
    return resp.json().then(function (body) { return { ok: resp.ok, body: body }; });
  }).then(function (res) {
    if (!res.ok) {
      var b = res.body || {};
      window.alert(action + " #" + pid + " failed: " +
        (b.status || "error") + (b.detail ? " — " + b.detail : ""));
    }
    refresh();
  }).catch(function () { refresh(); });
}

function toggleDiagnostics(panel, btn) {
  if (panel.dataset.loaded) {
    panel.hidden = !panel.hidden;
    return;
  }
  panel.dataset.loaded = "1";
  panel.hidden = false;
  panel.textContent = "loading diagnostics…";
  fetch("/api/diagnostics").then(function (r) { return r.json(); })
    .then(function (data) {
      var lines = [];
      var sources = (data && data.sources) || {};
      Object.keys(sources).forEach(function (name) {
        var s = sources[name] || {};
        lines.push("== " + name + " ==");
        lines.push(s.ok ? (s.output || "(empty)") : ("unavailable: " + (s.error || "?")));
        lines.push("");
      });
      panel.textContent = lines.join("\\n") || "no diagnostics";
    })
    .catch(function () {
      panel.textContent = "diagnostics fetch failed";
      delete panel.dataset.loaded;
    });
}

function renderCard(card, dupIndex) {
  var cls = "card state-" + card.state;
  if (card.duplicate) { cls += " dup-" + (dupIndex % 4); }
  var root = el("div", cls);

  var badges = el("div", "badges");
  badges.appendChild(el("span", "badge " + card.state, card.state));
  if (card.idle) { badges.appendChild(el("span", "badge idle", "idle")); }
  if (card.duplicate) {
    badges.appendChild(el("span", "badge dup", "dup " + (card.dup_group || "")));
  }
  if (card.sid && card.sid_verified === false) {
    badges.appendChild(el("span", "badge unverified", "sid?"));
  }
  root.appendChild(badges);

  root.appendChild(el("div", "identity", card.identity));
  root.appendChild(el("div", "cwd", card.cwd || "(no cwd)"));

  var meta = [card.shell, card.host].filter(Boolean).join(" · ");
  if (card.updated) { meta += (meta ? " · " : "") + "updated " + card.updated; }
  if (meta) { root.appendChild(el("div", "meta", meta)); }
  if (card.last_cmd) { root.appendChild(el("div", "lastcmd", "$ " + card.last_cmd)); }
  if (card.last_msg) { root.appendChild(el("div", "lastmsg", "\\u276f " + card.last_msg)); }

  var actions = el("div", "actions");
  actionsFor(card).forEach(function (action) {
    var btn = el("button", DESTRUCTIVE[action] ? "danger" : "", action);
    btn.addEventListener("click", function () { doAction(action, card.pid); });
    actions.appendChild(btn);
  });
  var panel = el("pre", "diagpanel");
  panel.hidden = true;
  var diagBtn = el("button", "diag", "why?");
  diagBtn.addEventListener("click", function () { toggleDiagnostics(panel, diagBtn); });
  actions.appendChild(diagBtn);
  root.appendChild(actions);
  root.appendChild(panel);
  return root;
}

function render(data) {
  var counts = data.counts || {};
  var countsBox = document.getElementById("counts");
  while (countsBox.firstChild) { countsBox.removeChild(countsBox.firstChild); }
  ["live", "ghost", "crashed", "idle", "duplicate"].forEach(function (name) {
    var span = el("span", "count");
    span.appendChild(el("b", "", counts[name] || 0));
    span.appendChild(document.createTextNode(name));
    countsBox.appendChild(span);
  });

  var main = document.getElementById("cards");
  while (main.firstChild) { main.removeChild(main.firstChild); }
  var cards = data.cards || [];
  var dupOrder = {};
  var nextDup = 0;
  cards.forEach(function (card) {
    var idx = 0;
    if (card.duplicate) {
      if (!(card.dup_group in dupOrder)) { dupOrder[card.dup_group] = nextDup++; }
      idx = dupOrder[card.dup_group];
    }
    main.appendChild(renderCard(card, idx));
  });
  document.getElementById("empty").hidden = cards.length !== 0;
}

function refresh() {
  fetch("/api/status").then(function (r) { return r.json(); })
    .then(function (data) {
      document.getElementById("offline").style.display = "none";
      render(data);
    })
    .catch(function () {
      document.getElementById("offline").style.display = "block";
    });
  fetch("/api/version").then(function (r) { return r.json(); })
    .then(function (v) {
      if (v && v.version !== PAGE_VERSION) { window.location.reload(); }
    })
    .catch(function () {});
}

refresh();
setInterval(refresh, POLL_MS);
</script>
</body>
</html>
"""

PAGE_HTML = _PAGE_TEMPLATE.replace("__PAGE_VERSION__", str(PAGE_VERSION))
