"""Claude-auth adapter — ask Claude Code itself whether it is signed in.

The ``AuthStatusSource`` port over ``claude auth status --json``, which
prints ``{"loggedIn": true, "authMethod": "oauth_token", ...}``.

Why CRR needs a second source at all: expiry detection started out
reading only ``~/.claude/.credentials.json``, and that file is not where
every host keeps its tokens. macOS Claude Code puts them in the login
Keychain; ``$CLAUDE_CONFIG_DIR`` moves the whole directory; an
enterprise gateway keeps nothing on disk. On any of those the file read
returns None forever, ``auth_state`` answers ``"unknown"``, and the
dashboard drew no badge and offered no Reauth button — CRR was blind to
an expired login on exactly the hosts where the user could least afford
it. This adapter is the fallback that sees those cases.

It is coarser than the file on purpose: ``loggedIn`` is a boolean with no
timestamps, so it can confirm an expiry but never warn ahead of one.
``crr.core.auth.resolve_auth_state`` owns that precedence; this module
only reports.

Tri-state and never raises, per the port: every failure path — missing
binary, timeout, nonzero exit, unparseable output, a ``loggedIn`` that
is not a bool — degrades to ``logged_in=None``, because none of them is
evidence of being signed out.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from crr.core.ports import AuthStatus

_CLAUDE_BIN = "claude"


class ClaudeAuthStatus:
    """``AuthStatusSource`` over the real ``claude`` CLI.

    ``timeout`` is injected (no default) so the value stays a named
    config prior at the composition root rather than a literal here —
    see ``claude_auth_probe_timeout_seconds`` in
    ``crr.core.config.DEFAULTS``. ``claude_bin`` exists for the same
    reason ``crr`` resolves its own service binary explicitly: a PATH
    lookup under a systemd unit is not the PATH the user's shell has.
    """

    def __init__(self, timeout: float, claude_bin: str | None = None):
        self._timeout = timeout
        self._bin = claude_bin or _CLAUDE_BIN

    def available(self) -> bool:
        try:
            return shutil.which(self._bin) is not None
        except Exception:
            return False

    def status(self) -> AuthStatus:
        argv = [self._bin, "auth", "status", "--json"]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return AuthStatus(None, "claude auth status timed out")
        except FileNotFoundError:
            return AuthStatus(None, f"{self._bin} not found")
        except Exception as exc:  # OSError and anything else — never raise
            return AuthStatus(None, f"claude auth status failed: {exc}")

        if result.returncode != 0:
            # A `claude` predating `auth status` exits nonzero. Not proof
            # of being signed out — the user may be perfectly logged in
            # with an older CLI.
            return AuthStatus(
                None, f"claude auth status exited {result.returncode}",
            )

        try:
            parsed = json.loads(result.stdout)
        except (ValueError, TypeError):
            return AuthStatus(None, "claude auth status output was not JSON")

        if not isinstance(parsed, dict):
            return AuthStatus(None, "claude auth status output was not an object")

        logged_in = parsed.get("loggedIn")
        # Strict bool check: a string "true" is not an answer, and
        # coercing one would invent the very claim this tri-state exists
        # to avoid. `isinstance(True, int)` is why bool is tested first
        # everywhere in this codebase — here the test is the other way
        # round, rejecting anything that is not literally a bool.
        if not isinstance(logged_in, bool):
            return AuthStatus(None, "claude auth status reported no loggedIn flag")

        method = parsed.get("authMethod")
        detail = (
            f"claude auth status: loggedIn={logged_in}"
            + (f", authMethod={method}" if isinstance(method, str) else "")
        )
        return AuthStatus(logged_in, detail)
