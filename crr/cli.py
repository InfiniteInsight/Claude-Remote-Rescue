"""crr command-line entry point — the composition root.

This is the ONE module allowed to import both ``crr.core`` and
``crr.adapters`` (the sole exception declared in .importlinter). Its job
is wiring: pick platform adapters, hand them to core, dispatch
subcommands. Business logic belongs in core, not here.

Phase 0 ships a deliberately thin CLI: enough to prove the entry point,
the layering, and the config-origin requirement are real. Session
operations (status/kick/close/reopen/dismiss/remove/diagnose) land in
Phase 1.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from crr import __version__
from crr.adapters import boot_identity  # composition root may import adapters
from crr.core import contracts  # ...and core


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crr",
        description="Keep Claude Code sessions alive and remotely rescuable.",
    )
    parser.add_argument("--version", action="version", version=f"crr {__version__}")
    sub = parser.add_subparsers(dest="command")

    diag = sub.add_parser("doctor", help="report scaffold/environment status")
    diag.set_defaults(func=_cmd_doctor)

    return parser


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Minimal liveness check: contracts import + boot-identity detection.

    Honest about maturity: this reports what Phase 0 can actually verify,
    not a green checkmark for features that do not exist yet.
    """
    print(f"crr {__version__} (Phase 0 scaffold)")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
