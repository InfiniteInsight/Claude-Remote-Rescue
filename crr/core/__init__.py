"""crr core — pure, platform-agnostic, dependency-free (stdlib only).

Holds the journal store, classifier, versioned contracts, and the port
(interface) definitions that adapters implement. This layer must not
import ``crr.adapters`` or ``crr.cli``; the import-linter contract in
``.importlinter`` fails the build if it ever does.
"""
