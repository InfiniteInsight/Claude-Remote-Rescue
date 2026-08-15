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
    """Classify how the control surface came up after the last boot.

    HEADLESS is judged on surface-near-boot ALONE — ``first_login`` is
    deliberately NOT a gate on it (design change, fix round 1). The reason
    is a false negative measured on the reference WSL host: Automatic
    Restart Sign-On (ARSO) establishes a LOCKED session at boot, so
    ``explorer.exe`` (the ``first_login`` proxy) starts at ~boot even
    though no human authenticated. That made ``surface_boot >= first_login``
    hold on EVERY boot, which pinned the verdict at ``login_only`` forever
    and told the user to reinstall a boot task that had, in fact, fired at
    boot+39s. A *proven* interactive unlock (Security 4624 type-2 / 4801)
    needs elevation — above crr's read-only ceiling — so ``first_login``
    cannot be salvaged as a headless gate and is used only to explain a
    LATE surface (the ``gap > window`` branch below).

    - ``machine_boot``/``surface_boot`` unreadable -> ``unknown`` (a null
      read must never render as a positive claim either way).
    - ``gap = surface_boot - machine_boot <= window_seconds`` -> ``headless``:
      the surface came up at boot, so the boot task fired. An early ARSO
      login does not change that and must not block it.
    - ``gap > window_seconds`` and a ``first_login`` lands within the window
      of the surface coming up -> ``login_only``: it only came up around a
      later login.
    - otherwise -> ``unknown``: too late to be boot-driven, no login near
      enough to explain it — crr cannot confirm a reboot survives.
    """
    if machine_boot is None or surface_boot is None:
        return BootVerdict("unknown", "could not read the boot timestamps")
    gap = surface_boot - machine_boot
    if gap <= window_seconds:
        return BootVerdict(
            "headless",
            f"the control surface came up {int(gap)}s after boot — the boot "
            "task fired; a reboot is survivable",
        )
    if first_login is not None and abs(surface_boot - first_login) <= window_seconds:
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
