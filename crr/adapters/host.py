"""Host/platform predicates that need I/O (so they can't live in pure core).

``is_wsl`` is a host fact, not a terminal or diagnostics concern — both the
tab-spawn selection and the diagnostics-source dispatch consult it, so it
lives here rather than inside one feature adapter.
"""

from __future__ import annotations


def is_wsl(proc_version_path: str = "/proc/version") -> bool:
    """True if this Linux userland is WSL (``/proc/version`` names microsoft)."""
    try:
        with open(proc_version_path, "r", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False
