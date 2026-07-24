"""crr command-line entry point — the composition root.

This is the ONE module allowed to import both ``crr.core`` and
``crr.adapters`` (the sole exception declared in .importlinter). Its job
is wiring: pick platform adapters, hand them to core, dispatch
subcommands. Business logic belongs in core, not here.

Phase 1 (in progress) adds ``status --json`` and ``config --effective``
on the shared core. The remaining session operations (kick/close/reopen/
dismiss/remove/diagnose) and the web dashboard follow.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable, Sequence

from crr import __version__
from crr.adapters import boot_identity  # composition root may import adapters
from crr.adapters import process_probe, state_dir, systemd, tmux
from crr.adapters.locking import mutation_lock
from crr.core import config as cfg  # ...and core
from crr.core import contracts, ops, reviver, status, web
from crr.core.archive import ArchiveStore
from crr.core.journal import JournalStore, new_entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crr",
        description="Keep Claude Code sessions alive and remotely rescuable.",
    )
    parser.add_argument("--version", action="version", version=f"crr {__version__}")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="report scaffold/environment status")
    doctor.set_defaults(func=_cmd_doctor)

    st = sub.add_parser("status", help="list journaled sessions and their state")
    st.add_argument("--json", action="store_true", help="emit the /api/sessions payload")
    st.set_defaults(func=_cmd_status)

    rev = sub.add_parser(
        "revive",
        help="revive crashed claude sessions into detached tmux (watchdog action)",
    )
    rev.set_defaults(func=_cmd_revive)

    # Session operations (classifier-gated, pid-keyed).
    rm = sub.add_parser("remove", help="delist a session (touches nothing else)")
    rm.add_argument("--pid", type=int, required=True)
    rm.set_defaults(func=_cmd_remove)

    dis = sub.add_parser("dismiss", help="clean up a crashed session without reviving")
    dis.add_argument("--pid", type=int, required=True)
    dis.set_defaults(func=_cmd_dismiss)

    reo = sub.add_parser("reopen", help="revive one specific crashed session now")
    reo.add_argument("--pid", type=int, required=True)
    reo.set_defaults(func=_cmd_reopen)

    w = sub.add_parser("web", help="serve the tailnet dashboard (loopback only)")
    w.add_argument("--port", type=int, default=8377)
    w.set_defaults(func=_cmd_web)

    sysd = sub.add_parser(
        "systemd",
        help="print (or --install) the systemd user watchdog timer + service",
    )
    sysd.add_argument("--install", action="store_true",
                      help="write units to ~/.config/systemd/user and enable the timer + web + linger")
    sysd.add_argument("--crr-bin", default=None,
                      help="absolute crr path to bake into the units (default: this crr binary)")
    sysd.add_argument("--port", type=int, default=8377,
                      help="dashboard port to bake into crr-web.service (default: 8377)")
    sysd.set_defaults(func=_cmd_systemd)

    conf = sub.add_parser("config", help="inspect configuration")
    conf.add_argument(
        "--effective",
        action="store_true",
        help="print every key with its value and origin (configured|default)",
    )
    conf.set_defaults(func=_cmd_config)

    # Shim-facing commands (called by the shell hooks, by absolute path).
    reg = sub.add_parser("register", help="[shim] journal a shell at start")
    reg.add_argument("--pid", type=int, required=True)
    reg.add_argument("--cwd", required=True)
    reg.add_argument("--shell", required=True, choices=contracts.SHELLS)
    reg.add_argument("--host", required=True, choices=contracts.HOSTS)
    reg.set_defaults(func=_cmd_register)

    lc = sub.add_parser("last-cmd", help="[shim] update a shell's last command / cwd")
    lc.add_argument("--pid", type=int, required=True)
    lc.add_argument("--cmd", required=True)
    lc.add_argument("--cwd", default=None)
    lc.set_defaults(func=_cmd_last_cmd)

    dereg = sub.add_parser("deregister", help="[shim] remove a shell's journal entry")
    dereg.add_argument("--pid", type=int, required=True)
    dereg.set_defaults(func=_cmd_deregister)

    cl = sub.add_parser(
        "claude-launch",
        help="[shim] journal a fresh claude session and print its session-id",
    )
    cl.add_argument("--pid", type=int, required=True)
    cl.add_argument("--session-id", default=None, help="use this sid instead of generating one")
    cl.set_defaults(func=_cmd_claude_launch)

    ce = sub.add_parser(
        "claude-exit",
        help="[shim] mark a shell's claude session ended (clean exit)",
    )
    ce.add_argument("--pid", type=int, required=True)
    ce.set_defaults(func=_cmd_claude_exit)

    shim = sub.add_parser(
        "shim",
        help="print the shell shim to source from your rc file",
    )
    shim.add_argument("shell", choices=SHIM_SHELLS)
    shim.add_argument(
        "--crr-bin",
        default=None,
        help="absolute path to bake into the shim (default: this crr binary)",
    )
    shim.set_defaults(func=_cmd_shim)

    return parser


# Shells that have a shim template. Keep in sync with the
# crr/shims/crr.<shell> files.
SHIM_SHELLS = ("bash", "zsh", "fish")


def _resolve_crr_bin(explicit: str | None) -> str:
    if explicit:
        return explicit
    # Prefer the absolute path of the crr that is generating the shim, so
    # the baked path points at exactly this install ([lesson: PATH
    # poisoning] — never a bare PATH lookup at shim runtime).
    argv0 = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.basename(argv0).startswith("crr") and os.path.exists(argv0):
        return argv0
    found = shutil.which("crr")
    return found or argv0 or "crr"


def _cmd_shim(args: argparse.Namespace) -> int:
    template = resources.files("crr.shims").joinpath(f"crr.{args.shell}").read_text(
        encoding="utf-8"
    )
    print(template.replace("@CRR_BIN@", _resolve_crr_bin(args.crr_bin)), end="")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"crr {__version__}")
    print(
        "contracts: journal v"
        f"{contracts.JOURNAL_SCHEMA_VERSION}, "
        f"sessions v{contracts.SESSIONS_CONTRACT_VERSION}, "
        f"diagnostics v{contracts.DIAGNOSTICS_CONTRACT_VERSION}"
    )
    try:
        adapter = boot_identity.detect()
        print(f"boot-identity adapter: {type(adapter).__name__}")
    except NotImplementedError as exc:
        print(f"boot-identity adapter: unavailable ({exc})")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = cfg.Config()
    store = JournalStore(state_dir.state_dir())
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr status: {exc}", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))

    scan = store.scan()
    payload = status.assemble_sessions(scan.entries, boot, probe)
    # Validate our own output before emitting it (the P7 validator doubles
    # as a debug guard — the server will run the same check).
    contracts.validate_sessions_payload(payload)

    # Corrupt files are surfaced on stderr, never silently dropped.
    for name, reason in scan.problems:
        print(f"crr status: skipped unreadable journal file {name}: {reason}", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_status_human(payload)
    return 0


def _print_status_human(payload: dict) -> None:
    sessions = payload["sessions"]
    if not sessions:
        print("no journaled sessions")
        return
    for card in sessions:
        dup = " [dup]" if card["duplicate_group"] else ""
        print(f"#{card['pid']} · {card['sid8']} [{card['state']}] {card['cwd']}{dup}")


def _cmd_register(args: argparse.Namespace) -> int:
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr register: {exc}", file=sys.stderr)
        return 2
    current_boot = boot.current()
    sd = state_dir.state_dir()
    store = JournalStore(sd)

    # Register-safety (recycled-pid guard for revival data): if an entry
    # already exists for this pid and carries a claude session, do not blindly
    # clobber it.
    claude = None
    tmux_session = None
    revive_strikes = 0
    with mutation_lock(sd):
        try:
            existing = store.read(args.pid)
        except (KeyError, contracts.ContractError):
            existing = None
        if existing is not None and existing.get("claude") is not None:
            if existing["boot_id"] != current_boot:
                # Different boot => the old process is unambiguously gone
                # (reboot or stale). Preserve its session in the archive so
                # the reviver can bring it back, then register fresh.
                ArchiveStore(sd).archive(existing, "superseded-on-register", _now())
            else:
                # Same boot => can't distinguish an rc re-source (this same
                # live shell) from pid reuse. Keep the claude field in place:
                # never wipe a possibly-live session, never risk a duplicate.
                claude = existing["claude"]
                tmux_session = existing["tmux_session"]
                revive_strikes = existing["revive_strikes"]

        store.write(new_entry(
            pid=args.pid,
            cwd=args.cwd,
            host=args.host,
            shell=args.shell,
            boot_id=current_boot,
            now=_now(),
            claude=claude,
            tmux_session=tmux_session,
            revive_strikes=revive_strikes,
        ))
    return 0


def _cmd_last_cmd(args: argparse.Namespace) -> int:
    # Hot-path prompt hook: if the entry is gone (never registered, already
    # deregistered), do nothing rather than error into the user's prompt.
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    with mutation_lock(sd):
        try:
            entry = store.read(args.pid)
        except (KeyError, contracts.ContractError):
            return 0
        entry["last_cmd"] = args.cmd
        if args.cwd is not None:
            entry["cwd"] = args.cwd
        entry["updated"] = _now()
        store.write(entry)
    return 0


def _cmd_deregister(args: argparse.Namespace) -> int:
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        JournalStore(sd).remove(args.pid)
    return 0


def _cmd_claude_launch(args: argparse.Namespace) -> int:
    # A wrapper-supplied or freshly generated sid is `injected` — certain,
    # never `guessed`. Print it (for the shim to pass to claude) even if the
    # shell was never registered, so claude still launches identifiably.
    sid = args.session_id or str(uuid.uuid4())
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    with mutation_lock(sd):
        try:
            entry = store.read(args.pid)
        except (KeyError, contracts.ContractError):
            entry = None
        if entry is not None:
            # If the entry already carries a claude session, it is a dead one
            # (the wrapper blocks while claude runs, and claude-exit clears the
            # field on any exit) — most often a reused pid. Preserve it in the
            # archive before overwriting, or its revival data is lost (the
            # symmetric hole to register-safety).
            if entry.get("claude") is not None and entry["claude"]["session_id"] != sid:
                ArchiveStore(sd).archive(entry, "superseded-on-launch", _now())
            entry["claude"] = {
                "session_id": sid,
                "sid_source": "injected",
                "started": _now(),
            }
            entry["updated"] = _now()
            store.write(entry)
    print(sid)
    return 0


def _cmd_claude_exit(args: argparse.Namespace) -> int:
    # Clean exit: clear the claude field. A crash skips this call, leaving
    # claude set so the reviver knows the shell died mid-session.
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    with mutation_lock(sd):
        try:
            entry = store.read(args.pid)
        except (KeyError, contracts.ContractError):
            return 0
        entry["claude"] = None
        entry["updated"] = _now()
        store.write(entry)
    return 0


def _cmd_revive(_args: argparse.Namespace) -> int:
    config = cfg.Config()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr revive: {exc}", file=sys.stderr)
        return 2
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    if not tmux_spawner.available():
        print("crr revive: tmux is required for revival but was not found", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    archive = ArchiveStore(sd)

    with mutation_lock(sd):
        scan = store.scan()
        outcome = reviver.revive_crashed(
            scan.entries, boot, probe, tmux_spawner, store, archive,
            max_strikes=config.get("zombie_strikes"),
            now=_now(),
        )
    for name, reason in scan.problems:
        print(f"crr revive: skipped unreadable journal file {name}: {reason}", file=sys.stderr)
    print(
        f"revived {len(outcome.revived)}, "
        f"gave up {len(outcome.gave_up)}, "
        f"already running {len(outcome.reset)}"
    )
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        res = ops.remove(JournalStore(sd), args.pid)
    print(res.message)
    return 0 if res.ok else 1


def _cmd_dismiss(args: argparse.Namespace) -> int:
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr dismiss: {exc}", file=sys.stderr)
        return 2
    config = cfg.Config()
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        res = ops.dismiss(JournalStore(sd), ArchiveStore(sd), boot, probe, args.pid, _now())
    print(res.message, file=sys.stdout if res.ok else sys.stderr)
    return 0 if res.ok else 2


def _cmd_reopen(args: argparse.Namespace) -> int:
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr reopen: {exc}", file=sys.stderr)
        return 2
    config = cfg.Config()
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    if not tmux_spawner.available():
        print("crr reopen: tmux is required for revival but was not found", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        res = ops.reopen(JournalStore(sd), tmux_spawner, boot, probe, args.pid, _now())
    print(res.message, file=sys.stdout if res.ok else sys.stderr)
    return 0 if res.ok else 2


def make_web_handler(
    sessions_provider: Callable[[], dict],
    allowed_hosts: set[str],
    allowed_suffixes: tuple[str, ...],
    action_provider: Callable[[str, int], tuple[bool, str]] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build an http.server handler bound to the given dependencies.

    Thin adapter: it only marshals bytes to/from ``web.handle_request``
    (the pure core handler that owns routing + the security gate).
    """

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            resp = web.handle_request(
                method, path, self.headers, body,
                sessions_provider=sessions_provider,
                action_provider=action_provider,
                allowed_hosts=allowed_hosts,
                allowed_suffixes=allowed_suffixes,
            )
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            self.wfile.write(resp.body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, *_args) -> None:  # keep the poll path quiet
            pass

    return _Handler


def _cmd_web(args: argparse.Namespace) -> int:
    config = cfg.Config()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr web: {exc}", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    archive = ArchiveStore(sd)
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))

    def provider() -> dict:
        return status.assemble_sessions(store.scan().entries, boot, probe)

    def action_provider(op: str, pid: int) -> tuple[bool, str]:
        # Same classifier-gated ops the CLI uses — one implementation — and
        # under the same mutation lock, so a double-tapped button (two
        # handler threads) or a race with the revive timer can't interleave.
        with mutation_lock(sd):
            if op == "remove":
                res = ops.remove(store, pid)
            elif op == "dismiss":
                res = ops.dismiss(store, archive, boot, probe, pid, _now())
            elif op == "reopen":
                res = ops.reopen(store, tmux_spawner, boot, probe, pid, _now())
            else:
                return False, f"unknown op {op}"
        return res.ok, res.message

    # Host allowlist: loopback + this host's name + tailnet suffix. (config
    # extras arrive with the TOML config loader.)
    allowed = {"127.0.0.1", "localhost", "[::1]", socket.gethostname().lower()}
    handler = make_web_handler(provider, allowed, (".ts.net",), action_provider)

    # Bind loopback ONLY; the tailnet (or a user proxy) is the auth boundary.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"crr web: serving on http://127.0.0.1:{args.port}/ (loopback only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _cmd_systemd(args: argparse.Namespace) -> int:
    config = cfg.Config()
    crr_bin = _resolve_crr_bin(args.crr_bin)
    path, missing = systemd.resolve_service_path(crr_bin)
    # XDG_STATE_HOME baked so the service watches the SAME state dir the shims
    # write to (state_dir() is <XDG_STATE_HOME>/crr; bake its parent).
    state_home = str(state_dir.state_dir().parent)
    interval = config.get("watchdog_interval_seconds")
    units = {
        systemd.SERVICE_NAME: systemd.revive_service_unit(crr_bin, path, state_home),
        systemd.TIMER_NAME: systemd.revive_timer_unit(interval),
        systemd.WEB_SERVICE_NAME: systemd.web_service_unit(crr_bin, path, state_home, args.port),
    }

    if missing:
        print(
            "crr systemd: WARNING — not found on PATH: "
            f"{', '.join(missing)}; revived sessions will fail on exec until these resolve",
            file=sys.stderr,
        )

    if args.install:
        ud = systemd.unit_dir(Path.home())
        systemd.write_units(ud, units)
        for cmd in systemd.enable_commands():
            subprocess.run(cmd, check=False)
        print(f"installed watchdog + dashboard units to {ud} and enabled them")
        return 0

    # Default: print for inspection (no changes to the user manager).
    for name, text in units.items():
        print(f"# ---- {name} ----")
        print(text)
    print("# Install with:  crr systemd --install")
    print("# (writes the units above to ~/.config/systemd/user/ and runs:)")
    for cmd in systemd.enable_commands():
        print("#   " + " ".join(cmd))
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    if not args.effective:
        print("usage: crr config --effective", file=sys.stderr)
        return 2
    config = cfg.Config()
    for key, (value, origin) in sorted(config.effective().items()):
        print(f"{key} = {value!r}  ({origin})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
