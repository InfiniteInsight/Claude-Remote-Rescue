"""Shell shim templates (data, not logic).

Each ``crr.<shell>`` file is a dependency-free shell script sourced from
the user's rc file. It calls ``crr`` by an absolute path baked in at
generation time (``crr shim <shell>``), never via PATH lookup
([lesson: PATH poisoning]), and is a no-op if that binary is missing
([lesson] a shim must never error text into the prompt).

These files contain no Python and import nothing; they are read as
package data by ``crr.cli`` (the composition root).
"""
