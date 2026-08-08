"""Per-session kick-attempt history for the dropped-Remote-Control watchdog
(review fix-wave 2026-08-07, FIX 1 — CRITICAL).

`cli._kick_dropped_bridges` was stateless across sweeps: nothing carried
forward between one `crr revive` pass and the next. A kick does not reset
`bridge_since` (the marker is still N records back — it takes a fresh
bridge marker to clear "dropped", and that only happens if the relaunch
actually reconnects), so a FAILED reconnect (host briefly offline, auth
expired, Remote Control unavailable) re-qualifies for another kick on
every subsequent pass — every guard the watchdog has clears again, because
none of them look at what happened last time. Left unguarded this is an
indefinite restart loop: dozens of SIGTERMs over hours, until the marker
finally drifts past `bridge_scan_lines` and the state degrades to "off".

This module is the missing memory. Two independent guards, both gating the
SAME `sid`, never `pid` (a recycled pid must not inherit another session's
kick history — same reasoning as `crr.core.settings`):

- a COOLDOWN (`bridge_kick_cooldown_seconds`): never re-kick a sid within
  this many seconds of its last attempt, win or lose.
- a hard ATTEMPT CAP (`bridge_kick_max_attempts`): after this many
  consecutive attempts, stop trying and say so.

The attempt counter resets ONLY when that sid's bridge state is observed
to be `"ok"` again (a confirmed successful reconnect) — never on a timer.
A timer-based reset would just delay the loop, not stop it: the whole
point of the cap is "give up until something actually changed", and the
only evidence that something changed is the bridge coming back.

Same JSON-in-the-state-dir, atomic-write discipline as
`crr/core/exclusions.py` / `crr/core/settings.py`. UNLIKE those two,
`is_degraded()` here should gate the entire watchdog step CLOSED (mirrors
`SettingsStore.is_degraded()`'s reasoning): a corrupt file degrading
silently to "no history" would erase the exact protection this module
exists to provide, right when the file might be corrupt because of the
very loop it is meant to stop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic

FILENAME = "bridge_kicks.json"

# Bounds the map so a machine with many session ids over its lifetime does
# not grow this file forever. When full, the LEAST RECENTLY KICKED sid is
# evicted to make room for a new one (mirrors exclusions.py/settings.py's
# MAX_* bounds, but by recency here rather than a hard write-time refusal —
# this file is watchdog-internal bookkeeping, not user input to reject).
MAX_ENTRIES = 500

# How many past attempts to keep per sid (#35 — lineage). "Why was this
# restarted three times?" needs all three, so this must exceed the attempt
# cap; small, because this file is read on every watchdog sweep and lineage
# must not turn it into an unbounded append log.
MAX_ATTEMPT_LOG = 5


def kick_eligible(
    *, attempts: int, last_kick_ts: float | None, now: float,
    cooldown_seconds: float, max_attempts: int,
) -> tuple[bool, str | None]:
    """Pure decision: may `sid` be kicked again right now?

    ``(True, None)`` when eligible; ``(False, <human reason>)`` otherwise.
    The cap is checked FIRST: once reached, a cooldown-shaped message would
    wrongly imply "wait and it'll retry on its own", which is false past
    the cap (only a confirmed "ok" reset does that).
    """
    if attempts >= max_attempts:
        return False, (
            f"kick attempt cap reached ({attempts}/{max_attempts} consecutive "
            "attempts) — giving up until the bridge reports ok again"
        )
    if last_kick_ts is not None:
        elapsed = now - last_kick_ts
        if elapsed < cooldown_seconds:
            remaining = cooldown_seconds - elapsed
            return False, (
                f"cooldown active ({remaining:.0f}s remaining of "
                f"{cooldown_seconds:.0f}s since the last attempt)"
            )
    return True, None


class KickHistoryStore:
    """Read/write the watchdog's per-sid kick-attempt history."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / FILENAME

    def _read_checked(self) -> tuple[dict[str, Any], bool]:
        """``(data, degraded)``. ``degraded`` is True only when the file
        EXISTS but could not be understood — never for a missing file (the
        normal, never-kicked-anything case)."""
        if not self._path.exists():
            return {}, False
        try:
            data = read_json_file(self._path)
        except (OSError, ValueError):
            return {}, True
        if not isinstance(data, dict) or not isinstance(data.get("sessions", {}), dict):
            return {}, True
        if not contracts.store_version_ok(data, contracts.KICKS_STORE_VERSION):
            # Degraded, not empty (#36). Reading a version this build does
            # not understand as "no history" would erase the cooldown and
            # attempt cap — which IS the restart-loop protection.
            return {}, True
        return data, False

    def is_degraded(self) -> bool:
        """True when a stored file exists but cannot be understood — see the
        module docstring for why the watchdog must fail CLOSED on this."""
        return self._read_checked()[1]

    def _sessions(self) -> dict[str, Any]:
        data, degraded = self._read_checked()
        if degraded:
            return {}
        sessions = data.get("sessions", {})
        return sessions if isinstance(sessions, dict) else {}

    def attempts(self, sid: str) -> int:
        """Consecutive kick attempts recorded for ``sid`` since its last
        confirmed-``ok`` reset, or ``0`` if never kicked (or degraded)."""
        entry = self._sessions().get(sid)
        if not isinstance(entry, dict):
            return 0
        n = entry.get("attempts")
        return n if isinstance(n, int) and not isinstance(n, bool) else 0

    def last_kick_ts(self, sid: str) -> float | None:
        """The clock time of ``sid``'s most recent recorded attempt, or
        ``None`` if it has never been kicked (or is degraded)."""
        entry = self._sessions().get(sid)
        if not isinstance(entry, dict):
            return None
        ts = entry.get("last_kick_ts")
        return float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None

    def record_kick(
        self, sid: str, now: float, observation: dict[str, Any] | None = None,
    ) -> None:
        """Record one more attempt for ``sid`` — call this on EVERY kick
        attempt, success or failure alike: the cap counts attempts, not
        failures, because a same-sweep verdict on whether the relaunch
        actually reconnected does not exist yet (that only shows up as a
        fresh bridge marker on a LATER sweep).

        ``observation`` is the LINEAGE (#35): the state that justified this
        particular kick — the pid signalled, the bridge reading, and the
        thresholds in force at the time. Recording the thresholds matters
        as much as the reading: without them, changing
        ``bridge_stale_records`` later silently rewrites the history of
        every decision taken under the old value, and a stored conclusion
        you cannot regenerate from its inputs is a claim you cannot audit.

        The attempt log is bounded (``MAX_ATTEMPT_LOG``) and the counters
        are untouched — the cooldown and cap read ``attempts`` /
        ``last_kick_ts`` exactly as before.
        """
        sessions = dict(self._sessions())
        prior = sessions.get(sid)
        prior_attempts = prior.get("attempts", 0) if isinstance(prior, dict) else 0
        log = list(prior.get("log", [])) if isinstance(prior, dict) else []
        record: dict[str, Any] = {"at": now}
        if observation:
            record.update(observation)
        log.append(record)
        sessions[sid] = {
            "attempts": prior_attempts + 1,
            "last_kick_ts": now,
            "log": log[-MAX_ATTEMPT_LOG:],
        }
        if len(sessions) > MAX_ENTRIES:
            oldest_sid = min(
                (s for s in sessions if s != sid),
                # `or 0`: a reset entry carries last_kick_ts=None (#45); mixing
                # None with floats raises TypeError once the map fills.
                key=lambda s: sessions[s].get("last_kick_ts") or 0,
            )
            del sessions[oldest_sid]
        write_json_atomic(
            self._path, {"v": contracts.KICKS_STORE_VERSION, "sessions": sessions})

    def session_ids(self) -> list[str]:
        """Every sid with recorded history, most recently kicked first."""
        sessions = self._sessions()
        return sorted(
            (s for s in sessions if isinstance(sessions[s], dict)),
            key=lambda s: sessions[s].get("last_kick_ts") or 0,
            reverse=True,
        )

    def attempt_log(self, sid: str) -> list[dict[str, Any]]:
        """This sid's recorded attempts, oldest first (``[]`` if none).

        Empty for a legacy counter-only file written before #35 — an honest
        "no lineage recorded", never a reconstructed one.
        """
        entry = self._sessions().get(sid)
        if not isinstance(entry, dict):
            return []
        log = entry.get("log")
        return [a for a in log if isinstance(a, dict)] if isinstance(log, list) else []

    def last_attempt(self, sid: str) -> dict[str, Any] | None:
        """The most recent recorded attempt, or None."""
        log = self.attempt_log(sid)
        return log[-1] if log else None

    def record_outcome(self, sid: str, *, ok: bool, message: str) -> None:
        """Attach the kick's result to the attempt just recorded (#35).

        Separate from ``record_kick`` because the outcome does not exist
        yet when the attempt is counted — the attempt must be recorded
        BEFORE ``ops.kick`` runs (see ``cli._kick_dropped_bridges``'s
        try/finally: a kick that raises must still count, or the restart
        loop reopens). A no-op when nothing has been recorded, so a failure
        path that reaches here early cannot raise inside that finally.
        """
        sessions = dict(self._sessions())
        entry = sessions.get(sid)
        if not isinstance(entry, dict):
            return
        log = list(entry.get("log", [])) if isinstance(entry.get("log"), list) else []
        if not log or not isinstance(log[-1], dict):
            return
        log[-1] = {**log[-1], "outcome_ok": bool(ok), "outcome": str(message)}
        sessions[sid] = {**entry, "log": log}
        write_json_atomic(
            self._path, {"v": contracts.KICKS_STORE_VERSION, "sessions": sessions})

    def reset(self, sid: str, now: float | None = None) -> None:
        """Clear ``sid``'s attempt COUNTERS — call this ONLY when its bridge
        state is observed to be ``"ok"`` again (a confirmed reconnect), per
        the module docstring. A harmless no-op for a sid with no history.

        The LINEAGE survives (#45, found by the first live end-to-end run).
        This used to delete the whole entry, so a kick that WORKED erased
        its own record ~30s later when the watchdog observed the reconnect,
        leaving ``crr kicks --list`` able to show only failures. The
        successful case is the common one and the one most worth being able
        to explain afterwards. ``now`` appends the reconnect itself, which
        is the END of the story: without it the record stops at "kicked"
        and never says whether it worked.

        Counters are genuinely cleared, not decayed: a confirmed reconnect
        makes any later drop a NEW incident, so neither the attempt cap nor
        the cooldown may carry into it.

        Deliberately NOT called under ``mutation_lock`` (unlike
        ``record_kick``, which shares the lock the kick itself takes): this
        file has exactly one writer, the single-threaded ``crr revive``
        sweep in ``cli._kick_dropped_bridges`` — never a concurrent web
        POST handler, unlike ``settings.json`` (FIX 3's problem). Systemd's
        oneshot timer does not overlap invocations of the same unit, so
        there is no concurrent writer for this call to race.
        """
        sessions = dict(self._sessions())
        entry = sessions.get(sid)
        if not isinstance(entry, dict):
            return
        log = list(entry.get("log", [])) if isinstance(entry.get("log"), list) else []
        # Append the reconnect only on the TRANSITION to ok, never once per
        # sweep. `reset` runs on EVERY watchdog pass that observes a healthy
        # bridge — roughly every 30s — so an unconditional append filled the
        # bounded log with identical "reconnected" records and evicted the
        # kick that caused them. Measured live: the real kick record was
        # gone inside three minutes.
        already = bool(log) and isinstance(log[-1], dict) and log[-1].get("event") == "reconnected"
        if now is not None and not already:
            log.append({"at": now, "event": "reconnected"})
        if log:
            sessions[sid] = {
                "attempts": 0, "last_kick_ts": None, "log": log[-MAX_ATTEMPT_LOG:],
            }
        else:
            del sessions[sid]   # nothing to preserve — no empty shell
        write_json_atomic(
            self._path, {"v": contracts.KICKS_STORE_VERSION, "sessions": sessions})
