"""reachable-at-boot verdict (spec 2026-08-14) — pure, no I/O.

Given machine boot, control-surface boot, and earliest interactive login,
decide whether the surface came up headless (a reboot is survivable), only at
login (the boot task did not fire), or unknown (a timestamp was unreadable).
An unknown must never render as headless.
"""

import pytest

from crr.core.boot_survival import BootVerdict, interpret_boot

WIN = 300


def test_surface_up_seconds_after_boot_and_before_login_is_headless():
    # The reference host: machine boot t=0, surface up t=39, no login yet.
    v = interpret_boot(machine_boot=0.0, surface_boot=39.0, first_login=None,
                       window_seconds=WIN)
    assert v.status == "headless"


def test_surface_up_before_a_later_login_is_headless():
    v = interpret_boot(machine_boot=0.0, surface_boot=39.0, first_login=500.0,
                       window_seconds=WIN)
    assert v.status == "headless"


def test_early_arso_login_does_not_block_headless():
    # Fix round 1 regression, THE reference-host case. On a host with
    # Automatic Restart Sign-On (ARSO), Windows establishes a LOCKED session
    # at boot, so the first_login proxy (oldest explorer.exe) starts at
    # ~boot (t=21) even though NO human authenticated. The surface still
    # came up at boot+39s — the boot task demonstrably fired. An early
    # first_login BELOW surface_boot must NOT drag this to login_only:
    # judging "was a human here" by session/explorer existence is exactly
    # what ARSO defeats, so first_login is not a headless gate anymore.
    v = interpret_boot(machine_boot=0.0, surface_boot=39.0, first_login=21.0,
                       window_seconds=WIN)
    assert v.status == "headless"


def test_surface_up_only_at_login_is_login_only():
    # Machine booted at 0, nobody around; surface didn't come up until the
    # login 8 hours later. The boot task did not fire.
    v = interpret_boot(machine_boot=0.0, surface_boot=28800.0,
                       first_login=28795.0, window_seconds=WIN)
    assert v.status == "login_only"


def test_just_outside_the_window_with_no_login_is_unknown_not_headless():
    # Came up 10 min after boot, but no login recorded to explain it. We cannot
    # claim headless (too late) nor login_only (no login). Unknown.
    v = interpret_boot(machine_boot=0.0, surface_boot=600.0, first_login=None,
                       window_seconds=WIN)
    assert v.status == "unknown"


@pytest.mark.parametrize("m,s,l", [
    (None, 39.0, None),
    (0.0, None, None),
    (None, None, None),
])
def test_a_missing_timestamp_is_unknown(m, s, l):
    assert interpret_boot(m, s, l, WIN).status == "unknown"


def test_verdict_is_frozen():
    import dataclasses
    v = interpret_boot(0.0, 39.0, None, WIN)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.status = "x"
