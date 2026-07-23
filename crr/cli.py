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
from typing import List, Optional

from . import bootid, journal, ops, revive
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
    stats = ops.gc()
    print(
        "gc: archived %(archived)d, pruned %(pruned)d archive file(s),"
        " removed %(tmp_removed)d tmp file(s)" % stats
    )
    return EXIT_OK


def cmd_diagnose(args: argparse.Namespace) -> int:
    # Real diagnostics adapters (journald / log show / Event Log) land in
    # later phases; report per-source so callers see the same shape.
    for source in ("boots", "prev_boot_errors", "host_events"):
        print("%s: not yet implemented" % source)
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


_PLUMBING = {"register", "update", "deregister"}


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
