"""CLI entry for branch_guard per the plumb-line hook I/O contract (v1).

The bundled python branch_guard.py ships only the decide() library — no
stdin/exit-code entry point (found inert during bootstrap verify,
2026-07-31). This shim supplies the contract: {"filePath": ...} on stdin,
branch from PLUMBLINE_BRANCH, config from PLUMBLINE_CFG (JSON keys
protectedBranches / docsAllowlist), exit non-zero with the reason on
stderr to block.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from branch_guard import decide  # noqa: E402

payload = json.loads(sys.stdin.read() or "{}")
cfg = json.loads(os.environ.get("PLUMBLINE_CFG") or "{}")
verdict = decide(
    payload.get("filePath", ""),
    os.environ.get("PLUMBLINE_BRANCH", ""),
    protected_branches=tuple(cfg.get("protectedBranches", ("main",))),
    docs_allowlist=tuple(cfg.get("docsAllowlist", ())),
)
if not verdict["allow"]:
    sys.stderr.write(verdict["reason"] + "\n")
    sys.exit(1)
sys.exit(0)
