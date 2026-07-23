"""`crr` command-line interface (argparse, stdlib only).

User-facing commands: status, kick, close, reopen, dismiss, remove,
revive, gc, diagnose.

Shim-facing plumbing: register, update, deregister. These run from shell
hooks on every prompt/exit, so they are silent on success and never print
noise on stderr in normal operation -- a hook that leaks error text into
the user's prompt is worse than one that fails quietly ([lesson: PATH
poisoning] adjacent: hooks must be invisible no-ops on failure).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import List, Optional

from . import bootid, install_shims, journal, ops, revive, service_linux, sidverify
from .result import EXIT_ERROR, EXIT_NOT_FOUND, EXIT_OK, OpResult, summarize


# ---------------------------------------------------------------------------
# Output helpers


def _print_result(res: OpResult, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(res.to_dict(), sort_keys=True))
    else:
        line = "%s pid=%s: %s" % (res.op, res.pid, res.status)
        if res.detail:
            line += " (%s)" % res.detail
        print(line)
    return res.exit_code


def _fmt_status_line(item: dict) -> str:
    claude_info = item.get("claude") or {}
    sid = claude_info.get("session_id") or "-"
    sid8 = sid[:8] if sid != "-" else "-"
    return "%-8s %-7s %-6s %-10s %s" % (
        item.get("pid"),
        item.get("state"),
        item.get("shell") or "-",
        sid8,
        item.get("cwd") or "-",
    )


# ---------------------------------------------------------------------------
# User-facing commands


def cmd_status(args: argparse.Namespace) -> int:
    items = ops.status()
    if args.json:
        print(json.dumps(items, indent=2, sort_keys=True))
    else:
        if not items:
            print("no sessions journaled")
        else:
            print("%-8s %-7s %-6s %-10s %s" % ("PID", "STATE", "SHELL", "SID", "CWD"))
            for item in items:
                print(_fmt_status_line(item))
    return EXIT_OK


def _pid_op(fn, args: argparse.Namespace) -> int:
    return _print_result(fn(args.pid), as_json=getattr(args, "json", False))


def cmd_revive(args: argparse.Namespace) -> int:
    if args.pid is not None:
        return _print_result(ops.reopen(args.pid))
    # `crr revive` / `crr revive --all`: revive every crashed entry with a sid.
    results = revive.revive_all()
    if not results:
        print("nothing to revive")
        return EXIT_OK
    for res in results:
        _print_result(res)
    return summarize(results)


def cmd_gc(args: argparse.Namespace) -> int:
    from . import config

    stats = ops.gc(config.load_config()["archive_retention_days"])
    print(
        "gc: archived %(archived)d, pruned %(pruned)d archive file(s),"
        " removed %(tmp_removed)d tmp file(s)" % stats
    )
    return EXIT_OK


def cmd_web(args: argparse.Namespace) -> int:
    from . import web  # local import: keep plumbing startup light

    return web.run(port=args.port)


def cmd_diagnose(args: argparse.Namespace) -> int:
    # Real diagnostics adapters (journald / log show / Event Log) land in
    # later phases; report per-source so callers see the same shape.
    for source in ("boots", "prev_boot_errors", "host_events"):
        print("%s: not yet implemented" % source)
    return EXIT_OK


def cmd_install_shims(args: argparse.Namespace) -> int:
    shells = args.shells or install_shims.detected_shells()
    if not shells:
        print(
            "crr install-shims: no supported shell (zsh/bash/fish) found on"
            " PATH",
            file=sys.stderr,
        )
        return EXIT_ERROR
    report = install_shims.install(shells)
    ok = True
    for shell, info in report.items():
        if info["error"]:
            ok = False
            print("%s: FAILED (%s)" % (shell, info["error"]), file=sys.stderr)
            continue
        bits = ["shim installed at %s" % info["shim_path"]]
        if info["rc_updated"]:
            bits.append("added source line to %s" % info["rc_path"])
        else:
            bits.append("%s already wired" % info["rc_path"])
        print("%s: %s" % (shell, "; ".join(bits)))
    return EXIT_OK if ok else EXIT_ERROR


def cmd_uninstall_shims(args: argparse.Namespace) -> int:
    shells = args.shells or list(install_shims.SHELLS)
    report = install_shims.uninstall(shells)
    ok = True
    for shell, info in report.items():
        if info["error"]:
            ok = False
            print("%s: FAILED (%s)" % (shell, info["error"]), file=sys.stderr)
        elif info.get("rc_cleaned"):
            print("%s: removed source line from %s" % (shell, info["rc_path"]))
        else:
            print("%s: nothing to remove in %s" % (shell, info["rc_path"]))
    return EXIT_OK if ok else EXIT_ERROR


def cmd_service_install(args: argparse.Namespace) -> int:
    crr_bin = install_shims.crr_bin_path()
    steps = service_linux.install(crr_bin)
    ok = True
    for step in steps:
        marker = "ok" if step["ok"] else "FAILED"
        detail = " (%s)" % step["detail"] if step["detail"] else ""
        print("%s: %s%s" % (step["step"], marker, detail))
        if not step["ok"]:
            ok = False
    return EXIT_OK if ok else EXIT_ERROR


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    steps = service_linux.uninstall()
    ok = True
    for step in steps:
        marker = "ok" if step["ok"] else "FAILED"
        detail = " (%s)" % step["detail"] if step["detail"] else ""
        print("%s: %s%s" % (step["step"], marker, detail))
        if not step["ok"]:
            ok = False
    return EXIT_OK if ok else EXIT_ERROR


def cmd_service_status(args: argparse.Namespace) -> int:
    for item in service_linux.status():
        print("%s: %s" % (item["unit"], item["active"]))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Shim-facing plumbing (silent on success, no stderr noise)


def cmd_register(args: argparse.Namespace) -> int:
    entry = journal.new_entry(
        pid=args.pid,
        cwd=args.cwd,
        shell=args.shell,
        host=args.host,
        boot_id=bootid.current_boot_id(),
    )
    journal.write_entry(entry)
    return EXIT_OK


def cmd_update(args: argparse.Namespace) -> int:
    entry = journal.read_entry(args.pid)
    if entry is None:
        return EXIT_NOT_FOUND  # silent: hooks race with deregistration
    if args.cwd is not None:
        entry["cwd"] = args.cwd
    if args.last_cmd is not None:
        entry["last_cmd"] = args.last_cmd
    if args.claude_sid is not None:
        claude_info = entry.get("claude") or {}
        claude_info["session_id"] = args.claude_sid
        claude_info.setdefault("started", journal.now_iso())
        entry["claude"] = claude_info
    if args.sid_verified is not None:
        claude_info = entry.get("claude") or {}
        claude_info["verified"] = args.sid_verified == "true"
        entry["claude"] = claude_info
    if args.tmux_session is not None:
        entry["tmux_session"] = args.tmux_session or None
    journal.write_entry(entry)
    return EXIT_OK


def cmd_deregister(args: argparse.Namespace) -> int:
    journal.delete_entry(args.pid)  # idempotent: missing entry is fine
    return EXIT_OK


def cmd_new_uuid(args: argparse.Namespace) -> int:
    """(plumbing) Print a fresh uuid4 -- used by the claude() wrapper to
    inject --session-id on fresh launches. Centralized here so shims stay
    dependency-free (no uuidgen / /proc assumptions)."""
    print(uuid.uuid4())
    return EXIT_OK


def cmd_now(args: argparse.Namespace) -> int:
    """(plumbing) Print the current time as a high-precision unix epoch.
    Used by the claude() wrapper to timestamp a launch before spawning the
    sid re-verification background check. `date +%s` (whole-second
    resolution, and %N for sub-second is a GNU-only date extension) is
    not precise enough: a picker-guess transcript written in the same
    wall-clock second as the launch can look "newer" than a
    whole-second-truncated launch time and get spuriously verified."""
    import time

    print("%.6f" % time.time())
    return EXIT_OK


def cmd_guess_sid(args: argparse.Namespace) -> int:
    """(plumbing) Print the newest transcript's sid for a cwd, or nothing
    when there isn't one yet. Used by the claude() wrapper to guess a sid
    for a bare `claude --resume` (picker) launch."""
    sid = sidverify.guess_sid(args.cwd)
    if sid:
        print(sid)
    return EXIT_OK


def cmd_verify_sid(args: argparse.Namespace) -> int:
    """(plumbing) Re-verify a guessed sid after the picker window has
    passed. Invoked in the background right after a bare `claude --resume`
    launch; sleeps out the wait itself so the shim doesn't have to."""
    sidverify.verify_sid(args.pid, args.started, wait_seconds=args.wait)
    return EXIT_OK


def cmd_resume_argv(args: argparse.Namespace) -> int:
    """(plumbing) Print the claude resume argv words (one per line, tail
    only -- no leading "claude") for a journaled pid's sid. Used by the
    shim's kick repair loop to relaunch with the right (possibly
    since-verified) sid without duplicating revive.build_claude_argv's
    verified/unverified fallback logic in shell."""
    entry = journal.read_entry(args.pid)
    if entry is None:
        return EXIT_NOT_FOUND
    argv = revive.build_claude_argv(entry)
    if argv is None:
        return EXIT_NOT_FOUND
    for word in argv[1:]:  # skip the leading "claude"
        print(word)
    return EXIT_OK


def cmd_take_relaunch_flag(args: argparse.Namespace) -> int:
    """(plumbing) Atomically check-and-clear the kick relaunch flag for a
    pid. Exit 0 when a flag was present (and is now cleared), EXIT_NOT_FOUND
    otherwise -- used both to clear stale flags and to decide whether the
    shim's repair loop should relaunch."""
    had = journal.take_relaunch_flag(args.pid)
    return EXIT_OK if had else EXIT_NOT_FOUND


_PLUMBING = {
    "register",
    "update",
    "deregister",
    "new-uuid",
    "now",
    "guess-sid",
    "verify-sid",
    "resume-argv",
    "take-relaunch-flag",
}


# ---------------------------------------------------------------------------
# Parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crr",
        description="Claude-Remote-Rescue: keep Claude Code sessions alive"
        " and remotely rescuable.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="list journaled sessions with state")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_status)

    for name, fn, help_text in (
        ("kick", ops.kick, "restart claude in place (same conversation)"),
        ("close", ops.close, "remote equivalent of typing exit"),
        ("reopen", ops.reopen, "revive one crashed session"),
        ("dismiss", ops.dismiss, "clean up without restoring (archives)"),
        ("remove", ops.remove, "pure delist; touches nothing"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("pid", type=int)
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=lambda args, _fn=fn: _pid_op(_fn, args))

    p = sub.add_parser("revive", help="revive crashed sessions into tmux")
    p.add_argument("pid", type=int, nargs="?", help="revive a single entry")
    p.add_argument(
        "--all", action="store_true", help="revive all crashed entries (default)"
    )
    p.set_defaults(func=cmd_revive)

    p = sub.add_parser("gc", help="archive dead entries, prune old archives")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("diagnose", help="why did my session die (stub)")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser(
        "web",
        help="serve the dashboard on loopback (expose via tailscale serve)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="listen port (default: config web_port, else 8377)",
    )
    p.set_defaults(func=cmd_web)

    p = sub.add_parser(
        "install-shims", help="install shell shims + rc-file wiring"
    )
    p.add_argument(
        "--shell",
        dest="shells",
        action="append",
        choices=list(install_shims.SHELLS),
        help="restrict to a specific shell (repeatable); default: autodetect"
        " shells present on this host",
    )
    p.set_defaults(func=cmd_install_shims)

    p = sub.add_parser(
        "uninstall-shims", help="remove the rc-file source lines crr added"
    )
    p.add_argument(
        "--shell",
        dest="shells",
        action="append",
        choices=list(install_shims.SHELLS),
        help="restrict to a specific shell (repeatable); default: all",
    )
    p.set_defaults(func=cmd_uninstall_shims)

    p = sub.add_parser(
        "service", help="systemd user units (Linux): web dashboard + watchdog"
    )
    service_sub = p.add_subparsers(dest="service_command", required=True)

    sp = service_sub.add_parser(
        "install", help="write units, daemon-reload, enable+start, enable-linger"
    )
    sp.set_defaults(func=cmd_service_install)

    sp = service_sub.add_parser("uninstall", help="disable and remove the units")
    sp.set_defaults(func=cmd_service_uninstall)

    sp = service_sub.add_parser("status", help="show each unit's active state")
    sp.set_defaults(func=cmd_service_status)

    p = sub.add_parser("register", help="(plumbing) create/overwrite an entry")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--shell", required=True)
    p.add_argument("--host", required=True)
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("update", help="(plumbing) update entry fields")
    p.add_argument("pid", type=int)
    p.add_argument("--cwd")
    p.add_argument("--last-cmd", dest="last_cmd")
    p.add_argument("--claude-sid", dest="claude_sid")
    p.add_argument("--sid-verified", dest="sid_verified", choices=("true", "false"))
    p.add_argument("--tmux-session", dest="tmux_session")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("deregister", help="(plumbing) delete an entry")
    p.add_argument("pid", type=int)
    p.set_defaults(func=cmd_deregister)

    p = sub.add_parser("new-uuid", help="(plumbing) print a fresh uuid4")
    p.set_defaults(func=cmd_new_uuid)

    p = sub.add_parser(
        "now", help="(plumbing) print the current time (high-precision unix epoch)"
    )
    p.set_defaults(func=cmd_now)

    p = sub.add_parser(
        "guess-sid", help="(plumbing) print the newest transcript sid for a cwd"
    )
    p.add_argument("cwd")
    p.set_defaults(func=cmd_guess_sid)

    p = sub.add_parser(
        "verify-sid",
        help="(plumbing) re-verify a guessed sid after the picker window",
    )
    p.add_argument("pid", type=int)
    p.add_argument(
        "--started",
        type=float,
        required=True,
        help="claude launch time (unix epoch seconds)",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=sidverify.DEFAULT_WAIT_SECONDS,
        help="seconds to wait before checking (default %.0f)"
        % sidverify.DEFAULT_WAIT_SECONDS,
    )
    p.set_defaults(func=cmd_verify_sid)

    p = sub.add_parser(
        "resume-argv",
        help="(plumbing) print resume argv words (one per line) for a pid",
    )
    p.add_argument("pid", type=int)
    p.set_defaults(func=cmd_resume_argv)

    p = sub.add_parser(
        "take-relaunch-flag",
        help="(plumbing) atomically check-and-clear a kick relaunch flag",
    )
    p.add_argument("pid", type=int)
    p.set_defaults(func=cmd_take_relaunch_flag)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    plumbing = args.command in _PLUMBING
    try:
        code = args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        if plumbing:
            # Shell hooks: never leak error text into the user's prompt.
            return EXIT_ERROR
        print("crr %s: error: %s" % (args.command, exc), file=sys.stderr)
        return EXIT_ERROR
    return code


if __name__ == "__main__":
    sys.exit(main())
