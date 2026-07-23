"""Structured operation results.

Every session operation returns an OpResult instead of printing or raising:
the CLI maps it to an exit code, and the (future) web layer serializes it
as JSON. [lesson] A swallowed exit code once turned hard failures into
green checkmarks -- failure statuses must propagate as distinct values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# Exit codes (stable API for shims, services, and scripts).
EXIT_OK = 0
EXIT_ERROR = 1  # unexpected internal failure
EXIT_NOT_FOUND = 2  # no journal entry for that pid
EXIT_REFUSED = 3  # classifier-gated refusal (wrong state for this op)
EXIT_FAILED = 4  # operation attempted but did not land
EXIT_NO_TMUX = 5  # revival requested but tmux is unavailable
EXIT_GAVE_UP = 6  # give-up guard: entry archived instead of re-revived


@dataclass
class OpResult:
    op: str
    pid: Optional[int]
    ok: bool
    status: str  # short machine-readable status, e.g. "kicked", "refused-live"
    state: Optional[str] = None  # classifier state at decision time
    detail: str = ""  # human-readable elaboration
    exit_code: int = EXIT_OK
    extra: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


def summarize(results: List[OpResult]) -> int:
    """Worst exit code across *results* (0 only if all ok)."""
    code = EXIT_OK
    for res in results:
        if res.exit_code > code:
            code = res.exit_code
    return code
