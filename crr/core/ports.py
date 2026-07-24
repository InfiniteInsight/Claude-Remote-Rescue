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

from typing import Protocol, Sequence, runtime_checkable


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


@runtime_checkable
class TmuxSpawner(Protocol):
    """The revival substrate: detached tmux sessions.

    ``list_sessions`` is a single batched read (one subprocess for all
    names) so the reviver can gate on live sessions without N spawns.
    ``new_detached_session`` takes argv **word-form**, never a shell
    string: a string is wrapped in the login shell, which re-sources the
    shim and double-registers the session ([lesson: word-form exec]).
    """

    def list_sessions(self) -> set[str]:
        """Return the set of currently-live tmux session names."""
        ...

    def new_detached_session(self, name: str, cwd: str, argv: Sequence[str]) -> None:
        """Create a detached session ``name`` in ``cwd`` running ``argv``."""
        ...
