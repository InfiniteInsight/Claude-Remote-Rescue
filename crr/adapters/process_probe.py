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


# Windows liveness. `os.kill(pid, 0)` is NOT an existence check there:
# CPython routes anything that is not CTRL_C_EVENT/CTRL_BREAK_EVENT through
# TerminateProcess, so the probe kills what it was asked about. Measured on
# CI, not inferred — the child came back with exit 0xC0000142 after nothing
# but an is_alive() call ([#74]). For a tool whose whole job is rescuing
# sessions, a status read that ends them is the worst available bug, so
# Windows asks the kernel instead of signalling.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _windows_is_alive(pid: int) -> bool:
    """True while ``pid`` is running, without touching it.

    ``STILL_ACTIVE`` is ambiguous by Windows' own design: a process that
    exits with code 259 is indistinguishable from a running one. That is a
    documented wart of GetExitCodeProcess, not something introduced here,
    and the alternative (a wait-with-zero-timeout on the handle) needs
    SYNCHRONIZE rights this deliberately does not ask for. Erring toward
    "alive" also errs toward not reviving a session twice.
    """
    import ctypes  # Windows-only, and lazily — see locking.py / #70
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without explicit restypes a 64-bit HANDLE truncates to int, which
    # leaks the handle and can still look truthy — a plausible wrong answer
    # is worse than an error.
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False  # gone, or never existed
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


class PsProcessProbe:
    def __init__(self, timeout_seconds: float) -> None:
        # Sourced from config (interop_timeout_seconds) by the caller.
        self._timeout = timeout_seconds

    def is_alive(self, pid: int) -> bool:
        if os.name == "nt":
            return _windows_is_alive(pid)
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


def _child_groups(
    rows: list[tuple[int, int, int, str]], shell_pid: int,
    include_shell_group: bool = False,
) -> list[int]:
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
    own_group_has_claude = False
    for _pid, ppid, pgid, argv0 in rows:
        if not (ppid == shell_pid and pgid > 0 and _is_claude_argv0(argv0)):
            continue
        if pgid == shell_pgid:
            # A claude child sharing the journaled process's OWN group means
            # that process runs no job control — it is the reviver's `sh -c`
            # exit-hook wrapper [/exit revival 2026-08-24], never an interactive shell (which
            # always gives claude its own group). Signalling shell_pgid here
            # ends the whole throwaway pane, which is the kill target. It is
            # gated on include_shell_group so this can NEVER fire for a live
            # user shell: only kick on a tmux-parked entry opts in.
            own_group_has_claude = True
        elif pgid not in groups:
            groups.append(pgid)
    if include_shell_group and own_group_has_claude and shell_pgid not in groups:
        groups.append(shell_pgid)
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


def _group_states_cmd() -> list[str]:
    return ["ps", "-A", "-o", "pgid=,stat="]


def _group_has_a_runnable_member(pgid: int, timeout: float = 5.0) -> bool | None:
    """True if any process in ``pgid`` is not a zombie. None if ps can't say.

    ``ps`` rather than /proc so this holds on macOS too, matching the other
    probes in this module.
    """
    try:
        result = subprocess.run(
            _group_states_cmd(), capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            row_pgid = int(parts[0])
        except ValueError:
            continue
        if row_pgid == pgid and not parts[1].strip().startswith("Z"):
            return True
    return False


def _group_alive(pgid: int) -> bool:
    """Does this group still contain a process that can RUN?

    A zombie has exited and is only waiting to be reaped: it cannot run, and
    it cannot be killed. ``killpg(pgid, 0)`` succeeds for one anyway, so this
    used to report a fully-dead group as alive for the whole grace window —
    and ``terminate_group`` then escalated to SIGKILL every time. Linux
    tolerates that against a zombie-only group; macOS returns EPERM, which
    surfaced as `kick`/`close` reporting "failed to signal" for a kill that
    had landed (#65).

    The killpg probe stays as the cheap first answer: ProcessLookupError is
    a definitive "nothing there" with no subprocess. Only when something
    exists is ps consulted to ask whether any of it can still run — and an
    unreadable ps is treated as alive, since "could not tell" must not be
    read as "safe to stop waiting".
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # NOT "exists but not ours" — measured on macOS CI, killpg(pgid, 0)
        # returns EPERM for a group whose only member is a zombie
        # (rows-for-pgid=[' 3391 Z<  '], ps rc=0). Returning True here
        # short-circuited the ps check entirely, which is why the zombie fix
        # worked on Linux and changed nothing on macOS (#65). Fall through
        # and let ps answer: a genuinely foreign LIVE group still reads
        # alive, because ps will show a non-zombie member.
        pass
    runnable = _group_has_a_runnable_member(pgid)
    return True if runnable is None else runnable


class PsProcessController:
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def claude_groups(self, shell_pid: int, *, include_shell_group: bool = False) -> list[int]:
        try:
            result = subprocess.run(
                _ps_snapshot_argv(),
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if result.returncode != 0:
            return []
        return _child_groups(
            _parse_ps_rows(result.stdout), shell_pid,
            include_shell_group=include_shell_group,
        )

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
            except PermissionError:
                # Not a permission problem: the SIGTERM above proved this
                # group is ours. macOS returns EPERM for a group whose only
                # remaining members are zombies — exited, unreaped, and
                # unkillable — where Linux quietly succeeds (#65). Treating
                # it as a failed kill made `crr kick`/`close` report "failed
                # to signal" on macOS for a kill that had already landed.
                pass
