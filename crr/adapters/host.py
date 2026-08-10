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


# `wslpath -w /` renders this distro's root as a Windows UNC path, and the
# first component after the host is the distro's CURRENTLY REGISTERED name:
#   \\wsl.localhost\Ubuntu-24.04\      (current WSL)
#   \\wsl$\Ubuntu-22.04\               (older builds)
_UNC_ROOTS = ("\\\\wsl.localhost\\", "\\\\wsl$\\")


def distro_name_from_wslpath(unc: str) -> str | None:
    """Parse the distro name out of ``wslpath -w /`` output, or None.

    Pure string work, split out from the subprocess call so the parsing is
    testable without a WSL host.
    """
    for root in _UNC_ROOTS:
        if unc.startswith(root):
            name = unc[len(root):].strip("\\").split("\\", 1)[0].strip()
            return name or None
    return None


def _wslpath_root(timeout: float | None = None) -> str | None:
    """``wslpath -w /`` output, or None if it cannot be run.

    ``wslpath`` is a Linux-side binary (/usr/bin/wslpath) — no interop, no
    Windows process — so this stays cheap and works inside a systemd service.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["wslpath", "-w", "/"], capture_output=True, text=True,
            timeout=timeout if timeout is not None else 5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def distro_name(env, timeout: float | None = None) -> str | None:
    """This distro's registered name: ask the system, fall back to the env.

    ``crr systemd`` bakes ``WSL_DISTRO_NAME`` into the unit because a service
    inherits no such variable — but a baked value goes stale silently, and a
    renamed distro leaves the tab spawner targeting one that no longer exists
    (#54). ``wslpath`` reports the current name, so it wins; the baked env is
    kept as the fallback so a host where ``wslpath`` is missing behaves
    exactly as it did before. None means "say nothing", and the caller omits
    ``--distribution`` rather than guessing.
    """
    return distro_name_from_wslpath(_wslpath_root(timeout) or "") or (
        env.get("WSL_DISTRO_NAME") or None
    )
