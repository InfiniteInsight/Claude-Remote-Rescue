"""Should crr hold this machine awake right now? (spec 2026-08-12)

Pure: no I/O, no clock, no platform. The cli owns the probes and the
holder; this module owns the policy, so every reason to withhold is
testable without a laptop to unplug.

``withheld`` is not decoration. "crr is holding nothing" is useless to a
user who enabled the feature and expects protection — the reason is the
whole message, and it is what `crr doctor` prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# C0 control range (0x00-0x1F) plus DEL (0x7F): newlines, carriage
# returns, and the ESC (0x1B) that introduces every ANSI escape sequence
# are all in here. Stripping ESC alone is enough to neutralize an escape
# sequence -- what's left behind is just visible text (e.g. "[31m"), not
# something a terminal or a doctor-style `[ok  ] label` line parser will
# ever treat as structure.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(text: str) -> str:
    """Strip control characters from an untrusted string before it is
    trusted enough to print (fix round 3, 2026-08-13).

    ``reason`` and every ``held`` item cross a process boundary through a
    JSON file `crr power`/`crr doctor` do not control the contents of --
    type-checking them (str) is not the same as content-checking them.
    An embedded newline followed by a fake ``"[ok  ] some other check"``
    line would forge doctor's checklist output; a raw ANSI escape would
    corrupt whatever terminal is reading it. Both are the same failure
    family as the rest of this module (an untrusted claim standing in for
    a real one), just aimed at OUTPUT STRUCTURE instead of the verdict.
    """
    return _CONTROL_CHARS_RE.sub("", text)


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


class _Unreadable:
    """Sentinel: the snapshot file EXISTS but could not be trusted — invalid
    JSON, or valid JSON that isn't an object. Deliberately not ``None``.

    Fix round 2 (2026-08-13) measured why the distinction matters: with a
    hold genuinely active, truncating ``power.json`` made ``read()``
    return ``None`` the same as a file that never existed — and
    ``interpret`` read that as the KNOWN-nothing branch, printing
    "holding: nothing" while a real hold was live. Absent and unparseable
    are different claims: absent means no loop has ever written anything;
    unparseable may be hiding a real, currently active hold that a crash
    mid-write or a filesystem fault clipped. Only ``crr.adapters.
    power_state.read`` constructs this; ``interpret`` is the only thing
    licensed to turn it into a message.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNREADABLE"


UNREADABLE = _Unreadable()


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
    dead writer, a report too old to still describe reality, or a
    snapshot too malformed to trust at all. Empty ``held`` with
    ``unknown`` unset is a POSITIVE claim: crr really is holding nothing,
    and ``reason`` says why (which may itself be "no keep-awake loop has
    reported" — a known nothing, not a guess).

    ``never_reported`` is ``True`` on ONLY the "no file at all" branch —
    distinct from ``unknown``, which means a report exists but can't be
    trusted right now. It exists so a caller (``crr doctor``) can tell
    "the loop is off, as configured" from "the loop is supposed to be
    running and never has been" without parsing ``reason`` text to find
    out — a positive claim belongs in a typed field, not something a
    caller reconstructs by string-matching.
    """
    held: frozenset[str]
    reason: str | None
    unknown: str | None
    never_reported: bool = False


def _malformed(data: dict) -> str | None:
    """Why a present, parsed dict cannot be trusted as a snapshot, or
    ``None`` if its shape is fine.

    Every field is checked before any of it is used — `crr power` and
    `crr doctor` both call this indirectly through `interpret`, and
    fix round 2 measured the alternative: `{"held": 5, ...}` raised an
    uncaught ``TypeError`` out of both commands, and `{"held": "sleep"}`
    (a string is iterable, so nothing here crashed) silently rendered as
    "holding: e, l, p, s". A shape check is not optional decoration; it is
    what stands between an untrusted file on disk and a claim this
    process prints as fact. ``v`` is checked too — a version field nothing
    ever reads is worse than no version field, because it LOOKS like
    protection.
    """
    if data.get("v") != POWER_SNAPSHOT_VERSION:
        return (f"the last report is snapshot version {data.get('v')!r}, "
                f"expected {POWER_SNAPSHOT_VERSION}")
    held = data.get("held")
    if not isinstance(held, list) or not all(isinstance(h, str) for h in held):
        return "the last report's held set is malformed"
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return "the last report's pid is malformed"
    updated = data.get("updated")
    if not isinstance(updated, (int, float)) or isinstance(updated, bool):
        return "the last report has no timestamp"
    return None


def interpret(
    data: dict | None,
    now: float,
    pid_alive: bool,
    max_age_seconds: float,
) -> Report:
    """Turn a raw (possibly absent, unreadable, malformed, orphaned, or
    stale) snapshot into a Report.

    In order, and the first one a `None` (or ``UNREADABLE``) answers:

    - No file at all -> a KNOWN nothing (no loop has ever reported), not
      an unknown -- `crr awake` has genuinely never run, or has always
      been off. ``unknown`` stays ``None``, ``never_reported`` is ``True``.
    - The file exists but couldn't be parsed (``data is UNREADABLE``), or
      parsed to something that isn't even a JSON object -> UNKNOWN. A
      corrupt file might be hiding a real, currently active hold.
    - The parsed dict's SHAPE is wrong (missing/mistyped ``held``/``pid``/
      ``updated``, or a ``v`` that doesn't match ``POWER_SNAPSHOT_VERSION``)
      -> UNKNOWN, via `_malformed`. Never a raise, never a rendered
      garbage set.
    - The writer pid is dead -> UNKNOWN. The file may describe reality at
      the moment it was written, but nothing has updated it since the one
      process that could have released the hold vanished — reporting its
      last claim as still true would be exactly the "succeeds loudly,
      protects nothing" failure this feature exists to end, just moved
      into the reporting path instead of the holding path.
    - The timestamp is older than ``max_age_seconds`` -> UNKNOWN, for the
      same reason: a loop that has stopped POLLING (hung, wedged, blocked
      on I/O) is not proven to still hold what it last wrote either.

    Only past all five does the recorded ``held``/``reason`` get trusted.
    """
    if data is None:
        return Report(frozenset(), "no keep-awake loop has reported", None,
                      never_reported=True)
    if data is UNREADABLE or not isinstance(data, dict):
        return Report(frozenset(), None,
                      "the last report could not be read "
                      "(power.json is corrupt or unreadable)")
    reason_malformed = _malformed(data)
    if reason_malformed:
        return Report(frozenset(), None, reason_malformed)
    if not pid_alive:
        return Report(frozenset(), None,
                      "the keep-awake loop that wrote this is gone")
    age = now - data["updated"]
    if age > max_age_seconds:
        return Report(frozenset(), None, f"last report is {int(age)}s old")
    # Sanitized HERE, not left to every render call site: `_cmd_power`'s
    # plain prints and `crr doctor`'s `_check(...)` lines are two
    # different render paths, and requiring both to remember to sanitize
    # is how this kind of gap reappears. Every consumer of a `Report`
    # gets a `held`/`reason` that is already safe to print verbatim.
    held = frozenset(_sanitize(h) for h in data["held"])
    raw_reason = data.get("reason")
    reason = _sanitize(raw_reason) if isinstance(raw_reason, str) else None
    return Report(held, reason or None, None)
