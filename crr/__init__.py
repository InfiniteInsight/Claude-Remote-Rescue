"""Claude-Remote-Rescue (`crr`).

Keep Claude Code sessions alive and remotely rescuable when terminals,
shells, or whole hosts die. See DESIGN.md for the architecture and the
audit-derived requirements this package is built to satisfy.

Layering (machine-enforced by import-linter — see .importlinter):

    crr.cli        composition root; the ONLY module allowed to import
                   both core and adapters.
        │  imports
        ▼
    crr.adapters   platform adapters; implement core ports. May import
                   core, never cli.
        │  imports
        ▼
    crr.core       journal store, classifier, contracts, ports. Imports
                   neither adapters nor cli.

The DESIGN diagram's arrows (core → adapters) are runtime call flow, the
*inverse* of the import direction enforced here.
"""

__version__ = "0.0.0"
