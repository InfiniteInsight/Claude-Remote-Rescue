"""Session classifier — computes live / ghost / crashed at read time.

Three states (DESIGN.md "Classifier"):

- ``live``    — same boot AND pid alive AND owns a controlling terminal.
- ``ghost``   — same boot, pid alive, but NO controlling terminal. Closing
                a terminal window can orphan the shell; without this state
                the dashboard shows healthy sessions that don't exist
                ([lesson: window-close orphans]).
- ``crashed`` — pid dead OR boot-identity mismatch.

The boot check comes FIRST and short-circuits: on a boot mismatch the host
rebooted, so the journaled pid may now belong to an unrelated process.
Probing it would be meaningless at best and, for any destructive operation
built on this, dangerous ([lesson: recycled pids]).

Pure core: takes a BootIdentity and a ProcessProbe (ports), so it is fully
testable with fakes and never touches the OS directly.
"""

from __future__ import annotations

from typing import Any, Mapping

from crr.core.ports import BootIdentity, ProcessProbe

LIVE = "live"
GHOST = "ghost"
CRASHED = "crashed"


# Separates the kernel boot id from PID 1's start time in a Linux identity
# (`<kernel boot_id>@<init start ticks>`, #138).
BOOT_ID_SEPARATOR = "@"


def same_boot(recorded: str | None, current: str | None) -> bool:
    """Whether a recorded boot identity names the current boot.

    The ONE comparison for boot identities — never ``==`` them directly.
    Exact match decides, except when exactly one side is a legacy bare
    kernel id (journaled before #138 added the PID-1 half): then the kernel
    halves decide. That keeps every pre-upgrade entry matching its own boot
    — a mass mismatch on upgrade would classify live sessions crashed and
    the reviver would spawn a second agent onto each one.
    """
    if recorded is None or current is None:
        return False
    if recorded == current:
        return True
    rec_compound = BOOT_ID_SEPARATOR in recorded
    cur_compound = BOOT_ID_SEPARATOR in current
    if rec_compound == cur_compound:
        return False
    return (recorded.split(BOOT_ID_SEPARATOR, 1)[0]
            == current.split(BOOT_ID_SEPARATOR, 1)[0])


def classify(
    entry: Mapping[str, Any],
    boot_identity: BootIdentity,
    process_probe: ProcessProbe,
) -> str:
    """Return the classifier state for ``entry``: LIVE, GHOST, or CRASHED."""
    if not same_boot(entry["boot_id"], boot_identity.current()):
        return CRASHED  # host rebooted; pid is not consulted (recycled-pid guard)

    pid = entry["pid"]
    if not process_probe.is_alive(pid):
        return CRASHED
    if process_probe.has_controlling_tty(pid):
        return LIVE
    return GHOST
