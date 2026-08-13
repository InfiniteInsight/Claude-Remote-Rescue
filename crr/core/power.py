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
