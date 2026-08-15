"""Linux side of reachable-at-boot: confirm linger, and read the boot/login
facts that back the same verdict the WSL adapter feeds.

On native Linux the control surface already comes up at boot via systemd
user linger (``crr-web.service`` is ``WantedBy=default.target`` and
``loginctl enable-linger`` lets the user manager run it without an active
login -- see ``crr.adapters.systemd``). This module's job is READ-only: it
confirms linger is actually set, and supplies the same
machine-boot/surface-boot/first-login timestamps
``crr.adapters.boot_windows.BootFacts`` carries, so the shared verdict logic
works unchanged on both platforms. Nothing here enables linger or starts a
unit -- that is ``crr.adapters.systemd``'s and the cli's job, after
confirmation.

Every read funnels through an injectable ``run`` (matching
``harden_windows.read_state``'s shape) so tests never spawn a real
``loginctl``/``systemctl``/``last``/``date``. The spine rule holds here
exactly as it does on Windows (spec 2026-08-14, Task 4): a read failure is
an honest ``unknown``, never a guess that could render a false "headless"
-- ``linger_enabled`` returns ``None`` (not ``False``) when ``loginctl``
fails, and ``read_facts`` collapses to an all-``None`` ``BootFacts`` on any
unhandled exception.

**Timezone conversion is always delegated to ``date -d``, never done by
this module's own datetime math against a bare wall-clock string.**
``systemctl show``'s ``ActiveEnterTimestamp`` and ``who``'s login column
are printed in LOCAL time with a timezone abbreviation (or none at all)
that Python's ``strptime``/``%Z`` cannot be trusted to round-trip -- the
exact scar ``boot_windows`` already paid for (its module docstring:
``Get-Date -UFormat %s`` "stringifies the local wall-clock digits as if
they were already UTC instead of converting"). Reproducing that mistake
here would corrupt the ONE delta this whole feature exists to measure --
did the surface come up shortly after machine boot -- since ``machine_boot``
(from ``/proc/stat``/``stat``) is a true epoch and a timezone-mangled
``surface_boot`` next to it silently produces a bogus multi-hour gap
instead of the real ~tens-of-seconds one. ``last --time-format iso``'s
output sidesteps the problem entirely (an explicit UTC offset on every
line, parsed with ``datetime.fromisoformat``); the two fields that cannot
avoid a bare local string (``ActiveEnterTimestamp``, ``who``) are handed
off to ``date -d ... +%s`` instead, which lets the same host that PRINTED
the local string do its own conversion.
"""

from __future__ import annotations

import re
from datetime import datetime

from crr.adapters._proc import run_capture as _run
from crr.adapters.boot_windows import BootFacts, parse_epoch
from crr.adapters.systemd import WEB_SERVICE_NAME

# No config injection point on this interface, matching
# boot_windows._INTEROP_TIMEOUT_SECONDS: mechanical timing for an
# unelevated interop read, not a policy a user would tune.
_INTEROP_TIMEOUT_SECONDS = 10.0


def _parse_linger(text: str) -> bool | None:
    """Parse ``loginctl show-user --property=Linger``'s one line.

    Only an exact ``Linger=yes``/``Linger=no`` is a KNOWN answer; anything
    else (empty, garbled, an unrecognized property dump) is ``None`` --
    never guessed either way.
    """
    line = text.strip().splitlines()[0] if text.strip() else ""
    if line == "Linger=yes":
        return True
    if line == "Linger=no":
        return False
    return None


def linger_enabled(user: str, run=None) -> bool | None:
    """Whether ``loginctl enable-linger`` is set for ``user``.

    ``run`` is injectable (signature ``(argv, timeout) -> str``) so tests
    never spawn a real ``loginctl``. Any exception -- missing binary, no
    dbus/logind session (a real failure mode on some containers/WSL
    configurations), timeout, nonzero exit -- yields ``None``, not
    ``False``: a read failure must never be reported as "linger is off".
    """
    run = run or _run
    try:
        text = run(
            ["loginctl", "show-user", user, "--property=Linger"],
            _INTEROP_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    return _parse_linger(text)


def _date_to_epoch(run, timeout: float, value: str) -> float | None:
    """Convert a local timestamp string to epoch by asking the HOST's own
    ``date`` to do it, rather than this module guessing an offset.

    Deliberately not wrapped in try/except -- a failure here has no
    further fallback and is meant to propagate to ``read_facts``'s
    top-level catch.
    """
    text = run(["date", "-d", value, "+%s"], timeout)
    return parse_epoch(text)


def _machine_boot_epoch(run, timeout: float) -> float | None:
    """System boot epoch: ``/proc/stat``'s ``btime`` line, falling back to
    ``stat -c %Z /proc/1`` (PID 1's ctime == when this instance booted) if
    the primary read fails to run or yields no ``btime`` line.

    The fallback call is deliberately NOT wrapped in its own try/except --
    if it also fails, that exception is meant to propagate out to
    ``read_facts``'s top-level catch, collapsing the whole result rather
    than reporting a machine_boot next to fields that came from a probe
    that -- if the fallback failed too -- has demonstrably stopped
    working. Both sources are already epoch-seconds; no timezone math is
    involved here at all.
    """
    try:
        text = run(["cat", "/proc/stat"], timeout)
    except Exception:
        text = None
    if text:
        for line in text.splitlines():
            if line.startswith("btime"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return float(parts[1])
                    except ValueError:
                        break
    text = run(["stat", "-c", "%Z", "/proc/1"], timeout)
    return parse_epoch(text)


def _active_enter_value(text: str) -> str | None:
    """Extract the raw value out of ``ActiveEnterTimestamp=<value>``.

    No date parsing here -- the value (if any) is handed to ``date -d``
    verbatim. An empty value means the unit has never activated, which is
    a KNOWN "no surface boot yet", not a probe failure -- returned as
    ``None`` with no further call.
    """
    line = text.strip().splitlines()[0] if text.strip() else ""
    prefix = "ActiveEnterTimestamp="
    if not line.startswith(prefix):
        return None
    value = line[len(prefix):].strip()
    return value or None


def _surface_boot_epoch(run, timeout: float) -> float | None:
    """``crr-web.service``'s ``ActiveEnterTimestamp``, resolved to epoch --
    when the dashboard's own unit last came up, which is the fact the
    verdict actually needs (not just that the machine booted).
    """
    text = run(
        ["systemctl", "--user", "show", WEB_SERVICE_NAME,
         "-p", "ActiveEnterTimestamp"],
        timeout,
    )
    value = _active_enter_value(text)
    if value is None:
        return None
    return _date_to_epoch(run, timeout, value)


_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def _find_iso_timestamp(line: str) -> str | None:
    """Locate the ISO-8601-with-offset token in a ``last`` line by shape,
    not by column position -- username/tty/host widths vary."""
    for token in line.split():
        if _ISO_TS_RE.match(token):
            return token
    return None


def _parse_last_output(text: str | None, floor: float | None) -> float | None:
    """Earliest login epoch across ``last -F --time-format iso``'s lines
    for ONE user (the caller already scoped ``last`` to ``user``), bounded
    below by ``floor`` (``machine_boot``) when known.

    The floor matters: unlike ``boot_windows``'s ``first_login`` (the
    oldest ``explorer.exe`` process, which by construction cannot predate
    this Windows session), ``last`` walks wtmp back across EVERY previous
    boot. Without the floor this would report the account's oldest login
    ever -- nonsensical as a "did the surface come up promptly at boot"
    signal, and a decidedly false one on a host that has been running for
    months. A line that does not fit the expected shape (an ISO token
    present) is skipped rather than raising -- partial, best-effort
    information beats discarding every other line over one garbled entry.
    """
    if not text:
        return None
    earliest: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("wtmp begins"):
            continue
        token = _find_iso_timestamp(line)
        if token is None:
            continue
        try:
            dt = datetime.fromisoformat(token)
        except ValueError:
            continue
        epoch = dt.timestamp()
        if floor is not None and epoch < floor:
            continue
        if earliest is None or epoch < earliest:
            earliest = epoch
    return earliest


def _parse_who_output(
    text: str, run, timeout: float, user: str, floor: float | None
) -> float | None:
    """Earliest login epoch for ``user`` across ``who``'s lines, bounded
    below by ``floor`` -- same reasoning as ``_parse_last_output``.

    ``who`` has no ``--time-format``/per-user filter of its own (that is
    ``last``-specific, util-linux), so this filters by the leading
    username column itself and converts each candidate's bare local
    ``YYYY-MM-DD HH:MM`` through ``date -d`` (never guessed). Used only as
    the fallback when ``last -F`` itself failed to run.
    """
    earliest: float | None = None
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 4 or parts[0] != user:
            continue
        date_str = f"{parts[2]} {parts[3]}"
        epoch = _date_to_epoch(run, timeout, date_str)
        if epoch is None:
            continue
        if floor is not None and epoch < floor:
            continue
        if earliest is None or epoch < earliest:
            earliest = epoch
    return earliest


def _first_login_epoch(
    run, timeout: float, user: str, machine_boot: float | None
) -> float | None:
    """Earliest login epoch for ``user`` since ``machine_boot``, from
    ``last -F --time-format iso <user>``, falling back to ``who`` if the
    primary command fails to run or yields nothing parseable.

    The ``who`` fallback call is NOT wrapped in its own try/except -- same
    reasoning as ``_machine_boot_epoch``'s ``stat`` fallback: if it also
    fails, that propagates to ``read_facts``'s top-level catch.
    """
    try:
        text = run(["last", "-F", "--time-format", "iso", user], timeout)
    except Exception:
        text = None
    result = _parse_last_output(text, machine_boot)
    if result is not None:
        return result
    text = run(["who"], timeout)
    return _parse_who_output(text, run, timeout, user, machine_boot)


def read_facts(user: str, run=None) -> BootFacts:
    """Gather the boot/login facts the reachable-at-boot verdict needs.

    ``run`` is injectable (signature ``(argv, timeout) -> str``) so tests
    never spawn real ``cat``/``stat``/``systemctl``/``date``/``last``/
    ``who`` processes. Any exception anywhere in this body -- a missing
    binary, a timeout, a nonzero exit from a call with no further fallback
    -- collapses the WHOLE result to an all-``None`` ``BootFacts`` (the
    spine rule: an unreadable timestamp renders as ``unknown`` in the
    verdict, never a guess that could produce a false "headless", spec
    2026-08-14 Task 4). ``locked`` and ``autologin`` are always ``None`` on
    Linux -- lock-screen/autologin state is not meaningful here, and the
    verdict does not require them for this platform.
    """
    run = run or _run
    try:
        machine_boot = _machine_boot_epoch(run, _INTEROP_TIMEOUT_SECONDS)
        surface_boot = _surface_boot_epoch(run, _INTEROP_TIMEOUT_SECONDS)
        first_login = _first_login_epoch(
            run, _INTEROP_TIMEOUT_SECONDS, user, machine_boot
        )
    except Exception:
        return BootFacts(None, None, None, None, None)
    return BootFacts(machine_boot, surface_boot, first_login, None, None)
