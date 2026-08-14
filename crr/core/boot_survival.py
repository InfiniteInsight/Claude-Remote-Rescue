"""Did crr's control surface come up at boot, headless? (spec 2026-08-14)

Pure: three timestamps in, a verdict out. No I/O, no platform. The adapters
read the clocks; this decides what they mean, and it keeps an unknown unknown
— a reboot reported "survivable" when it was not is the exact failure this
whole project is built against.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BootVerdict:
    status: str   # "headless" | "login_only" | "unknown"
    detail: str


def interpret_boot(
    machine_boot: float | None,
    surface_boot: float | None,
    first_login: float | None,
    window_seconds: int,
) -> BootVerdict:
    """Classify how the control surface came up after the last boot."""
    if machine_boot is None or surface_boot is None:
        return BootVerdict("unknown", "could not read the boot timestamps")
    gap = surface_boot - machine_boot
    came_up_before_login = first_login is None or surface_boot < first_login
    if gap <= window_seconds and came_up_before_login:
        return BootVerdict(
            "headless",
            f"the control surface came up {int(gap)}s after boot, "
            "before any login — a reboot is survivable",
        )
    if first_login is not None and surface_boot >= first_login:
        return BootVerdict(
            "login_only",
            "the control surface did not come up until login — the boot task "
            "did not fire; run `crr reachable-at-boot --install`",
        )
    return BootVerdict(
        "unknown",
        f"the control surface came up {int(gap)}s after boot with no login to "
        "explain it — cannot confirm it survives a reboot",
    )
