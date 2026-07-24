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

from typing import Protocol, runtime_checkable


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
