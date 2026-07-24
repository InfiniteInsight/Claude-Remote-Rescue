"""crr adapters — the five narrow platform interfaces.

boot-identity, tab-spawn, service-manager, diagnostics-source, and the
state-dir path resolver. Adapters implement ports declared in
``crr.core`` and may import ``crr.core``; they must not import
``crr.cli``. Platform-specific adapters detect their platform and skip
cleanly elsewhere (see DESIGN.md "Testing").
"""
