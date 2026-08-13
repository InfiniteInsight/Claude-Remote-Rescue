"""Should crr hold this machine awake right now? (spec 2026-08-12)

Pure: no I/O, no clock, no platform. The cli owns the probes and the
holder; this module owns the policy, so every reason to withhold is
testable without a laptop to unplug.

``withheld`` is not decoration. "crr is holding nothing" is useless to a
user who enabled the feature and expects protection — the reason is the
whole message, and it is what `crr doctor` prints.
"""

from __future__ import annotations

from dataclasses import dataclass

# What each config mode asks for. "sleep" means AUTOMATIC/idle sleep only:
# lid close is never in scope on any platform (see the spec — logind
# exempts the lid from inhibitors by default, and the Windows/macOS
# mechanisms only ever affected idle).
MODES: dict[str, frozenset[str]] = {
    "off": frozenset(),
    "sleep": frozenset({"sleep"}),
    "sleep+shutdown": frozenset({"sleep", "shutdown"}),
}


@dataclass(frozen=True)
class Decision:
    want: frozenset[str]        # subset of {"sleep", "shutdown"}
    reason: str                 # shown in the OS's own blocking UI
    withheld: str | None = None  # why nothing is held, for doctor


def decide(
    live_sessions: int,
    on_ac: bool | None,
    mode: str,
    requires_ac: bool,
) -> Decision:
    """Decide what to hold, or explain why nothing is held."""
    if mode not in MODES:
        return Decision(frozenset(), "",
                        f"power_block={mode!r} is not a recognised mode "
                        f"({', '.join(sorted(MODES))})")
    want = MODES[mode]
    if not want:
        return Decision(frozenset(), "", "power_block is off")
    if live_sessions <= 0:
        return Decision(frozenset(), "", "no live claude session")
    if requires_ac:
        if on_ac is None:
            # Not "assume AC" and not "assume battery" — either would be a
            # claim nothing measured.
            return Decision(frozenset(), "",
                            "cannot tell whether this machine is on AC")
        if not on_ac:
            return Decision(frozenset(), "",
                            "on battery (power_block_requires_ac is true)")
    plural = "" if live_sessions == 1 else "s"
    return Decision(want, f"crr: {live_sessions} Claude session{plural} live")


def unmet(capabilities: frozenset[str], want: frozenset[str]) -> tuple[str, ...]:
    """What was asked for that this platform cannot deliver.

    Exists so `crr doctor` can state the gap. Holding half of what was
    requested while reporting success is the failure mode this whole
    design keeps running into: a hold that succeeds loudly and protects
    nothing.
    """
    return tuple(sorted(want - capabilities))


# --- cross-process visibility (fix round 1, 2026-08-13) --------------------
#
# `crr power` / `crr doctor` run in a SEPARATE process from `crr awake`.
# The hold is a child of the awake loop; a freshly-constructed holder in a
# different process has no handle to it and `.held()` can only ever answer
# about a child THAT process spawned — never the real one. Asking it
# anyway measured as the bug: a real `crr awake` holding, sampled from a
# separate `crr power` invocation, reported "holding: nothing" while the
# hold was actually active (confirmed by a live holder child pid).
#
# The fix is a state file the awake loop stamps after every poll. This
# module owns only the PURE interpretation of that file's content — no
# I/O, no clock, no pid probing (all three are owned by the caller, same
# split as `decide()` above) — so every honesty rule below is testable
# without a loop to run or a process to kill.
POWER_SNAPSHOT_VERSION = 1


def snapshot(held: frozenset[str], reason: str, pid: int, updated: float) -> dict:
    """The record `crr awake` stamps to disk after every poll.

    ``pid`` is the AWAKE LOOP's own pid (its liveness is what a reader can
    actually check), not the hold's child process — crr has no portable,
    race-free way to name that child's identity across every platform, and
    the loop's own liveness is the fact that matters: if the loop that owns
    the hold is gone, whatever it held is unknown regardless of the
    child's own fate.
    """
    return {
        "v": POWER_SNAPSHOT_VERSION,
        "held": sorted(held),
        "reason": reason,
        "pid": pid,
        "updated": updated,
    }


@dataclass(frozen=True)
class Report:
    """What a reader in a SEPARATE process may honestly say is held.

    ``unknown`` and an empty ``held`` are different claims, and the whole
    point of this type is to keep them from collapsing into each other:
    ``unknown`` set means "the state file cannot be trusted right now" —
    dead writer, or a report too old to still describe reality. Empty
    ``held`` with ``unknown`` unset is a POSITIVE claim: crr really is
    holding nothing, and ``reason`` says why (which may itself be "no
    keep-awake loop has reported" — a known nothing, not a guess).
    """
    held: frozenset[str]
    reason: str | None
    unknown: str | None


def interpret(
    data: dict | None,
    now: float,
    pid_alive: bool,
    max_age_seconds: float,
) -> Report:
    """Turn a raw (possibly absent, orphaned, or stale) snapshot into a Report.

    Three questions, in order, and the first one a `None` answers:

    - No file at all -> a KNOWN nothing (no loop has ever reported), not
      an unknown -- `crr awake` has genuinely never run, or has always
      been off. ``unknown`` stays ``None``.
    - The writer pid is dead -> UNKNOWN. The file may describe reality at
      the moment it was written, but nothing has updated it since the one
      process that could have released the hold vanished — reporting its
      last claim as still true would be exactly the "succeeds loudly,
      protects nothing" failure this feature exists to end, just moved
      into the reporting path instead of the holding path.
    - The timestamp is older than ``max_age_seconds`` -> UNKNOWN, for the
      same reason: a loop that has stopped POLLING (hung, wedged, blocked
      on I/O) is not proven to still hold what it last wrote either.

    Only past all three does the recorded ``held``/``reason`` get trusted.
    """
    if data is None:
        return Report(frozenset(), "no keep-awake loop has reported", None)
    if not pid_alive:
        return Report(frozenset(), None,
                      "the keep-awake loop that wrote this is gone")
    updated = data.get("updated")
    if not isinstance(updated, (int, float)):
        return Report(frozenset(), None, "last report has no timestamp")
    age = now - updated
    if age > max_age_seconds:
        return Report(frozenset(), None, f"last report is {int(age)}s old")
    held = data.get("held") or ()
    return Report(frozenset(held), data.get("reason") or None, None)
