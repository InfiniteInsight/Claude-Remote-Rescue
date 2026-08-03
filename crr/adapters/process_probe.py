"""Process-probe adapter (implements crr.core.ports.ProcessProbe).

``is_alive`` uses ``os.kill(pid, 0)`` — portable across Linux/macOS, no
subprocess. ``has_controlling_tty`` shells out to ``ps -o tty= -p <pid>``
per DESIGN.md (portable, avoids /proc so it also works on macOS), guarded
by an interop timeout that the composition root sources from config
(never a magic number here).

If the tty check cannot be determined (timeout, ps missing, error), it
returns False: we degrade toward ``ghost``/``crashed`` rather than
claiming a session is ``live`` on unknown evidence.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Sequence

from crr.core.ports import ResumeProcess

# tty strings that mean "no controlling terminal".
_NO_TTY = {"?", "??"}


def _tty_is_real(raw: str) -> bool:
    """True if a `ps -o tty=` value denotes a real controlling terminal."""
    value = raw.strip()
    return bool(value) and value not in _NO_TTY


def _parse_tty_pids(stdout: str) -> set[int]:
    """Parse ``ps -o tty=,pid=`` output into the set of pids with a real tty.

    Each line is ``<tty> <pid>`` (tty first, pid last); a pid is included
    only when its tty column denotes a real controlling terminal.
    """
    out: set[int] = set()
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if _tty_is_real(parts[0]):
            try:
                out.add(int(parts[-1]))
            except ValueError:
                continue
    return out


class PsProcessProbe:
    def __init__(self, timeout_seconds: float) -> None:
        # Sourced from config (interop_timeout_seconds) by the caller.
        self._timeout = timeout_seconds

    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but we may not signal it — still alive
        return True

    def has_controlling_tty(self, pid: int) -> bool:
        try:
            result = subprocess.run(
                ["ps", "-o", "tty=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        if result.returncode != 0:
            return False
        return _tty_is_real(result.stdout)

    def controlling_ttys(self, pids: Sequence[int]) -> set[int]:
        ids = [int(p) for p in pids]
        if not ids:
            return set()  # never `ps` with no -p (it would list every process)
        try:
            result = subprocess.run(
                ["ps", "-o", "tty=,pid=", "-p", ",".join(str(p) for p in ids)],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return set()
        if result.returncode != 0:
            return set()
        return _parse_tty_pids(result.stdout)


def _ps_snapshot_argv() -> list[str]:
    # -A all processes; bare `=` headers -> no header line. args last so the
    # first three columns parse as ints and the remainder is the command line.
    return ["ps", "-A", "-o", "pid=,ppid=,pgid=,args="]


def _parse_ps_rows(stdout: str) -> list[tuple[int, int, int, str]]:
    rows: list[tuple[int, int, int, str]] = []
    for line in stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        argv0 = parts[3].split(None, 1)[0] if parts[3].strip() else ""
        rows.append((pid, ppid, pgid, argv0))
    return rows


def _is_claude_argv0(argv0: str) -> bool:
    """argv0 basename starts with 'claude' (claude, claude-fake, /path/claude).

    Scoped by ancestry (direct child of the journaled shell) — this is the
    claude-selection the port name promises, NOT a global cmdline pattern
    kill ([lesson: kill-by-ancestry] still holds).
    """
    base = argv0.rsplit("/", 1)[-1].lstrip("-")  # login shells prefix '-'
    return base.startswith("claude")


def _parse_ps_rows_full_args(stdout: str) -> list[tuple[int, int, int, str]]:
    """Same row shape as ``_parse_ps_rows`` (pid, ppid, pgid) but keeps the
    FULL args string as the fourth field instead of truncating it to argv0.

    ``_parse_ps_rows`` deliberately keeps only argv0 (all ``claude_groups``
    needs) — do not change that; ``find_resume_process`` instead needs the
    whole cmdline so it can find ``--resume <sid>`` as argv tokens further
    along the line, so this is a second, separate parser over the same
    ``ps -o pid=,ppid=,pgid=,args=`` snapshot shape.
    """
    rows: list[tuple[int, int, int, str]] = []
    for line in stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        rows.append((pid, ppid, pgid, parts[3]))
    return rows


def _child_groups(rows: list[tuple[int, int, int, str]], shell_pid: int) -> list[int]:
    shell_pgid = next((pgid for pid, _ppid, pgid, _a in rows if pid == shell_pid), None)
    if shell_pgid is None:
        return []
    groups: list[int] = []
    for _pid, ppid, pgid, argv0 in rows:
        if (ppid == shell_pid and pgid != shell_pgid and pgid > 0
                and pgid not in groups and _is_claude_argv0(argv0)):
            groups.append(pgid)
    return groups


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours (shouldn't happen for own sessions)


class PsProcessController:
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def claude_groups(self, shell_pid: int) -> list[int]:
        try:
            result = subprocess.run(
                _ps_snapshot_argv(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if result.returncode != 0:
            return []
        return _child_groups(_parse_ps_rows(result.stdout), shell_pid)

    def find_resume_process(self, session_id: str) -> ResumeProcess | None:
        """Locate a live ``claude --resume <session_id>`` process (`crr
        adopt --takeover`'s live-process resolver).

        One ``ps`` snapshot, matched on the FULL argv (via
        ``_parse_ps_rows_full_args`` — ``_parse_ps_rows`` truncates to
        argv0 for ``claude_groups``'s ancestry selector and must stay that
        way, so this is a separate parse of the same snapshot). A row
        matches when its argv0 basename starts with ``claude``
        (``_is_claude_argv0``, reused) AND its args, split on whitespace,
        contain both ``"--resume"`` and ``session_id`` as WHOLE tokens —
        never a substring check, so a row carrying a sid that is merely a
        prefix (or superstring) of ``session_id`` cannot false-hit.

        This sid-scoped ``--resume <UUID>`` match is a DIFFERENT
        specificity class from the broad ``_is_claude_argv0``-only
        ancestry selector the kill-by-ancestry lesson warns against
        (``_child_groups``/``claude_groups``): matching a specific UUID
        identifies one conversation, not "any process that looks like
        claude". It is still not trusted alone — the caller (cli)
        additionally re-checks the sid is untracked immediately before
        killing (closing the resolve-to-kill race) and signals by the
        returned ``pgid``, never by re-running this argv pattern at kill
        time.

        Returns the first match's ``(pid, ppid, pgid)``; ``None`` if no
        row matches, if ``ps`` exits non-zero, or if the subprocess call
        itself fails (mirrors ``claude_groups``'s degrade-to-empty policy —
        an inconclusive probe must never be read as "found").
        """
        try:
            result = subprocess.run(
                _ps_snapshot_argv(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        for pid, ppid, pgid, args in _parse_ps_rows_full_args(result.stdout):
            tokens = args.split()
            if not tokens or not _is_claude_argv0(tokens[0]):
                continue
            if "--resume" in tokens and session_id in tokens:
                return ResumeProcess(pid, ppid, pgid)
        return None

    def terminate_group(self, pgid: int, grace_seconds: float) -> None:
        os.killpg(pgid, signal.SIGTERM)  # raises OSError if undeliverable
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return
            time.sleep(0.1)
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # it died in the race between the check and the kill
