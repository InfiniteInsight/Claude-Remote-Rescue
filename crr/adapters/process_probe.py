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

    def claude_group_pids(self, shell_pids: Sequence[int]) -> dict[int, list[int]]:
        """Claude process-group ids per shell pid, from ONE ``ps`` snapshot.

        The read-only, batched counterpart to
        ``ProcessController.claude_groups`` (the parallel of
        ``controlling_ttys`` vs ``has_controlling_tty``). The reachability
        detector needs "does this state file's pid belong to a claude job
        under this journaled shell" for every card on every 5s poll;
        ``claude_groups`` forks a full ``ps -A`` per call, which the 30s
        watchdog can afford and the dashboard cannot — 17 cards would cost
        ~204 forks a minute, forever.

        It lives on the read-only probe deliberately: the status path must
        not be handed ``terminate_group`` just to answer a question about
        pids (see ``ports.ProcessController``'s docstring on the split).
        Same degrade-to-empty policy as every other probe here — an
        inconclusive ``ps`` yields ``{}``, which the caller reads as
        "unknown", never as a confirmed mismatch.
        """
        ids = [int(p) for p in shell_pids]
        if not ids:
            return {}
        try:
            result = subprocess.run(
                _ps_snapshot_argv(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return {}
        if result.returncode != 0:
            return {}
        rows = _parse_ps_rows(result.stdout)
        return {pid: _child_groups(rows, pid) for pid in ids}


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
    # A revived session is journaled under the claude process itself, which
    # has no claude children — claude_groups() would find nothing and Kick
    # would signal nothing (#58). Measured live: claude_groups(2016) == [].
    # A journaled shell is never claude, so the usual path is untouched.
    self_argv0 = next((a for pid, _ppid, _pgid, a in rows if pid == shell_pid), "")
    if _is_claude_argv0(self_argv0):
        return [shell_pgid] if shell_pgid > 0 else []
    groups: list[int] = []
    for _pid, ppid, pgid, argv0 in rows:
        if (ppid == shell_pid and pgid != shell_pgid and pgid > 0
                and pgid not in groups and _is_claude_argv0(argv0)):
            groups.append(pgid)
    return groups


def parent_of(pid: int, timeout: float = 5.0) -> int | None:
    """The parent pid of ``pid`` via ``ps``, or None if unknown.

    Backs `crr whoami`'s walk up to the journaled shell. Portable (``ps``,
    not /proc, so it also works on macOS) and degrades to None on any probe
    failure — the walk then reports "not found" rather than guessing.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    try:
        return int(raw)
    except ValueError:
        return None


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

    def resume_session_ids(self) -> set[str]:
        """Every session id that a live ``claude --resume <sid>`` is running.

        The batch counterpart to ``find_resume_process`` (and the direct
        parallel to ``controlling_ttys``): ONE ``ps`` snapshot answers "is this
        conversation already running?" for a whole page of discoverable rows,
        instead of one snapshot per row. The dashboard uses it to warn that
        plain Adopt would start a SECOND claude on a transcript that is still
        live, and to steer to Take over instead.

        Same degrade-to-empty policy as the other probes here: an
        inconclusive ``ps`` yields an empty set (no warnings shown) rather
        than a fabricated answer — the adopt path's own competing-resume
        disclosure remains the backstop.
        """
        try:
            result = subprocess.run(
                _ps_snapshot_argv(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return set()
        if result.returncode != 0:
            return set()
        sids: set[str] = set()
        for _pid, _ppid, _pgid, args in _parse_ps_rows_full_args(result.stdout):
            tokens = args.split()
            if not tokens or not _is_claude_argv0(tokens[0]):
                continue
            for i, tok in enumerate(tokens):
                if tok == "--resume" and i + 1 < len(tokens):
                    sids.add(tokens[i + 1])
        return sids

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
