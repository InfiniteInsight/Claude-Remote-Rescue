"""Ports — the narrow interfaces adapters implement (core owns these).

DESIGN.md names five platform adapter interfaces: boot identity, tab
spawn, service manager, diagnostics source, state-dir paths. Phase 0
declares only the boot-identity port so the layering graph is real and
the import-linter has something to enforce; the rest arrive with their
Phase-1+ implementations.

Ports live in core precisely so the dependency arrow points the right
way: adapters import core to implement these, never the reverse.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Protocol, Sequence, runtime_checkable


class ResumeProcess(NamedTuple):
    """The (pid, ppid, pgid) of a live ``claude --resume <sid>`` process.

    Returned by ``ProcessController.find_resume_process`` — the caller
    (cli) kills by ``pgid`` (never by re-matching the argv pattern) and
    arms the shim close-flag on ``ppid`` before the kill (see
    ``ProcessController.find_resume_process``'s docstring for why this
    argv match is a different specificity class from ``claude_groups``'s
    ancestry selector)."""

    pid: int
    ppid: int
    pgid: int


@runtime_checkable
class BootIdentity(Protocol):
    """Answers 'is this the same boot as when the entry was journaled?'.

    The classifier compares a journaled ``boot_id`` against ``current()``
    to distinguish a live/ghost session (same boot) from a crashed one
    (boot mismatch → the host rebooted).
    """

    def current(self) -> str:
        """Return an opaque, per-boot-stable identifier for this host."""
        ...


@runtime_checkable
class ProcessProbe(Protocol):
    """Answers liveness questions about a pid for the classifier.

    ``has_controlling_tty`` is what separates a ``live`` session from a
    ``ghost`` (pid alive but its terminal was closed). Implementations
    stay portable: DESIGN.md specifies ``ps -o tty= -p <pid>`` rather than
    ``/proc`` so the same adapter works on macOS.
    """

    def is_alive(self, pid: int) -> bool:
        """Return True if a process with ``pid`` currently exists."""
        ...

    def has_controlling_tty(self, pid: int) -> bool:
        """Return True if ``pid`` owns a controlling terminal."""
        ...

    def controlling_ttys(self, pids: Sequence[int]) -> set[int]:
        """Return the subset of ``pids`` that own a controlling terminal.

        One batched query for the poll path, so status assembly costs a
        single subprocess for the whole session list instead of one per card
        (DESIGN 'snap jq' performance requirement). Empty ``pids`` → empty
        set (never a bare ``ps`` that would list every process).
        """
        ...

    def claude_group_pids(self, shell_pids: Sequence[int]) -> dict[int, list[int]]:
        """Claude process-group ids per shell pid, from ONE process snapshot.

        The read-only, BATCHED counterpart to
        ``ProcessController.claude_groups`` (same relation as
        ``controlling_ttys`` to ``has_controlling_tty``). It is on the probe
        rather than the controller on purpose: the reachability detector on
        the poll path needs this answer for every card and must not be
        handed signalling power to get it.

        A shell pid with no claude jobs maps to an empty list. An
        inconclusive probe returns ``{}`` — the caller must read a missing
        or empty entry as "cannot confirm", never as a confirmed mismatch.
        """
        ...


@runtime_checkable
class ProcessController(Protocol):
    """Signal a live session's claude process group (a mutation — kept
    separate from the read-only ProcessProbe so read callers get no signal
    power). Discovery is by ancestry; signalling targets the whole group."""

    def claude_groups(self, shell_pid: int) -> list[int]:
        """Process-group ids of the shell's non-shell child jobs (claude).

        Excludes the shell's own group, so a returned pgid is always safe to
        signal without killing the shell. Empty when none / shell absent."""
        ...

    def terminate_group(self, pgid: int, grace_seconds: float) -> None:
        """SIGTERM the group, then SIGKILL it if still alive after the grace
        window. Raises OSError if the initial signal cannot be delivered."""
        ...

    def resume_session_ids(self) -> set[str]:
        """Every session id with a live ``claude --resume <sid>`` process.

        The BATCH counterpart to ``find_resume_process`` (parallel to
        ``controlling_ttys`` vs ``has_controlling_tty``): one probe answers
        "already running?" for a whole page of discoverable rows. An
        inconclusive probe degrades to an empty set, never a fabricated
        answer."""
        ...

    def find_resume_process(self, session_id: str) -> "ResumeProcess | None":
        """Locate a live ``claude --resume <session_id>`` process (`crr
        adopt --takeover`'s live-process resolver), or None if none is
        running.

        This is a sid-scoped, whole-argv-token match on a specific UUID —
        a DIFFERENT specificity class from ``claude_groups``'s
        ``_is_claude_argv0``-only ancestry selector, which the
        kill-by-ancestry lesson warns is too broad to signal from
        (any process that merely *looks like* claude). One UUID matches
        one conversation; there is no plausible false-positive class the
        way "any argv0 starting with claude" has. The caller still keeps
        two independent guards on top of this match rather than trusting
        it alone: it re-checks the sid is still untracked immediately
        before killing (closing the resolve-to-kill race), and it signals
        by the returned ``pgid`` — never by re-running this pattern match
        at kill time.
        """
        ...


@runtime_checkable
class TmuxSpawner(Protocol):
    """The revival substrate: detached tmux sessions.

    ``list_sessions`` is a single batched read (one subprocess for all
    names) so the reviver can gate on live sessions without N spawns.
    ``new_detached_session`` takes argv **word-form**, never a shell
    string: a string is wrapped in the login shell, which re-sources the
    shim and double-registers the session ([lesson: word-form exec]).
    """

    def session_pid(self, name: str) -> int | None:
        """The pid running in session ``name``'s first pane, or None.

        None means "could not be determined" and callers must not guess:
        this pid is what the journal is re-keyed onto (#58), so a wrong one
        would point every pid-keyed op at the wrong process.
        """
        ...

    def list_sessions(self) -> set[str] | None:
        """Return the set of currently-live tmux session names, or None.

        None means "could not be determined" (audit F16 — a timeout, an
        OSError, or a query exit that isn't tmux's own confident "there is
        no server" signal) — distinct from a genuine empty set (no server
        at all, hence no sessions). Callers must never collapse the two:
        treating an unknown state as "confirmed no sessions" risks a
        revive strike, a give-up archive, or a destructive op against a
        session that may in fact still be alive (spine — null-result
        expressibility).
        """
        ...

    def new_detached_session(self, name: str, cwd: str, argv: Sequence[str]) -> None:
        """Create a detached session ``name`` in ``cwd`` running ``argv``."""
        ...

    def kill_session(self, name: str) -> None:
        """Kill the tmux session ``name``. Raises on failure (e.g. the
        underlying ``tmux kill-session`` exiting nonzero) — callers that
        must not lose bookkeeping on a failed kill catch this themselves."""
        ...


class TabSpawnTimeout(Exception):
    """The spawn command did not finish in time — NOT proof no tab opened.

    Distinct from a hard failure on purpose. Launching a cold terminal app
    (Windows Terminal in particular) can outrun the budget while still
    succeeding a moment later, so callers must report this as "could not
    confirm" rather than asserting the tab failed (#53). Adapters raise it
    instead of letting a subprocess timeout surface as a generic error.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(f"no tab confirmed within {seconds:g}s")
        self.seconds = seconds


@runtime_checkable
class TabSpawner(Protocol):
    """Opens a *visible* terminal tab — the counterpart to detached tmux.

    DESIGN: revival lands in a detached tmux session (durable, survives the
    tab closing); a TabSpawner then attaches to it visibly ``where tabs
    exist`` (macOS Terminal/iTerm now, Linux desktop later). ``open_tab``
    takes argv **word-form**; the adapter is responsible for quoting it into
    whatever its mechanism needs (a shell string for ``osascript``), never
    the caller. It is best-effort: the caller has already made the session
    durable, so a raised error costs convenience, not state.
    """

    def available(self) -> bool:
        """Return True if this spawner's terminal app is present/usable."""
        ...

    def open_tab(self, argv: Sequence[str], cwd: str | None = None) -> None:
        """Open a visible tab running ``argv`` (optionally ``cd`` to cwd)."""
        ...


class DiagnosticsSource(Protocol):
    """Platform "why did it die" source (journald / log+pmset / winevent).

    Implemented by adapter *modules* (crr.adapters.diagnostics*), not
    classes. ``collect(config)`` returns
    ``(boots, prev_boot_errors, host_events, degraded)`` and degrades
    per-source rather than raising. ``config`` is typed ``Any`` here to
    avoid widening this module's imports; the real type passed at every
    call site is ``crr.core.config.Config``.
    """

    SOURCE_NAME: str

    def available(self) -> bool: ...

    def collect(self, config: Any) -> tuple[list, list, list, list]: ...


@runtime_checkable
class PowerSource(Protocol):
    """Is this machine on mains power?

    Tri-state ON PURPOSE. A machine with no battery device is a desktop —
    that is a known ``True``, not an unknown. ``None`` means the probe
    failed, and per the spine principle that must never be turned into
    either answer by a consumer.
    """

    def on_ac(self) -> bool | None: ...


@runtime_checkable
class PowerHolder(Protocol):
    """Keep the machine awake / un-restartable while held.

    Implementations hold via a CHILD PROCESS whose lifetime is the hold,
    so the hold cannot outlive crr. Same reasoning as
    ``crr.adapters.locking``: crr's whole purpose is surviving processes
    that die badly, and a hold that leaks would block restarts with
    nothing left running to explain why.
    """

    def capabilities(self) -> frozenset[str]:
        """Which of {"sleep", "shutdown"} this platform can actually do."""

    def hold(self, want: frozenset[str], reason: str) -> None:
        """Start (or update) the hold. Idempotent for an unchanged want."""

    def release(self) -> None:
        """Drop the hold. Safe to call when nothing is held."""

    def held(self) -> frozenset[str]:
        """What is currently held."""

    # OPTIONAL, and deliberately not declared as a method here:
    #
    #     def withheld(self) -> str | None
    #
    # "why part of the last request was dropped by THIS HOST's
    # configuration" -- distinct from `capabilities()`, which is what the
    # PLATFORM can do. Only `LinuxPowerHolder` has an answer (the
    # `LidSwitchIgnoreInhibited=no` case, and the unreadable-logind-config
    # case); the Mac and Windows holders have none, and inventing an
    # always-None one for them would be a claim, not an implementation.
    #
    # It is not declared because this Protocol is STRUCTURAL -- nothing
    # subclasses it, so a default body here would never run for the
    # holders that lack the method, and declaring it would make them
    # non-conforming for a method they have nothing to say through.
    # `crr.cli._holder_withheld` is the one reader, and it uses `getattr`.
