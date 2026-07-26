"""Shared subprocess plumbing for adapters.

One home for the "run a command, return stdout, RAISE on a nonzero exit"
pattern the diagnostics sources rely on — the guard that keeps a swallowed
exit code from masquerading as an empty-but-successful result.
"""

from __future__ import annotations

import subprocess
from typing import Sequence


def run_capture(argv: Sequence[str], timeout: float) -> str:
    """Run ``argv``, returning stdout; raise ``RuntimeError`` on nonzero exit."""
    result = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{argv[0]} exited {result.returncode}")
    return result.stdout
