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


def classify(
    entry: Mapping[str, Any],
    boot_identity: BootIdentity,
    process_probe: ProcessProbe,
) -> str:
    """Return the classifier state for ``entry``: LIVE, GHOST, or CRASHED."""
    if entry["boot_id"] != boot_identity.current():
        return CRASHED  # host rebooted; pid is not consulted (recycled-pid guard)

    pid = entry["pid"]
    if not process_probe.is_alive(pid):
        return CRASHED
    if process_probe.has_controlling_tty(pid):
        return LIVE
    return GHOST
