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
import sys
from typing import Sequence

from crr import __version__
from crr.adapters import boot_identity  # composition root may import adapters
from crr.adapters import process_probe, state_dir
from crr.core import config as cfg  # ...and core
from crr.core import contracts, status
from crr.core.journal import JournalStore


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

    conf = sub.add_parser("config", help="inspect configuration")
    conf.add_argument(
        "--effective",
        action="store_true",
        help="print every key with its value and origin (configured|default)",
    )
    conf.set_defaults(func=_cmd_config)

    return parser


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
