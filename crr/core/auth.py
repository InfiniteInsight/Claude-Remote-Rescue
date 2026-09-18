"""Auth state detection — pure core, no I/O.

Takes parsed credential data and a wall-clock timestamp, returns the
OAuth state and time until expiration. The file read happens in the CLI
layer; this module only decides.

Mirrors ``crr.core.reachability``: an adapter reads, core classifies.

TWO sources, not one (spec 2026-09-17, keychain-blind reauth).
``auth_state`` classifies the credentials FILE and is the only source
with timestamps, hence the only one that can warn ahead of time. But the
file is not universal: Claude Code keeps its tokens in the login Keychain
on macOS, under ``$CLAUDE_CONFIG_DIR`` when that is set, and nowhere on
disk at all under an enterprise gateway. On those hosts the file read can
only ever answer ``"unknown"`` — which the dashboard rendered exactly
like ``"valid"``, so an expired login showed no badge and offered no
Reauth button on the one screen the user had left.
``resolve_auth_state`` folds in the second, coarser source (``claude auth
status --json``'s ``loggedIn``) and reports WHICH one spoke.
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple

from crr.core.contracts import AUTH_SOURCES, AUTH_STATES

__all__ = [
    "AUTH_SOURCES",
    "AUTH_STATES",
    "EXPIRING_WINDOW_SECONDS",
    "AuthResolution",
    "auth_state",
    "resolve_auth_state",
]

EXPIRING_WINDOW_SECONDS = 3 * 24 * 3600  # 3 days


def auth_state(
    credentials: Mapping[str, Any] | None, *, now: float
) -> tuple[str, int | None]:
    """Classify the OAuth auth state from parsed credential timestamps.

    Returns ``(state, expires_in_seconds)`` where state is one of
    ``AUTH_STATES`` and ``expires_in_seconds`` is seconds until the
    earliest expiration (None when the state is ``"unknown"``; may be
    negative when the state is ``"expired"``).

    ``credentials`` is the parsed JSON from ``~/.claude/.credentials.json``.
    Timestamps (``expiresAt``, ``refreshTokenExpiresAt``) are Unix epoch
    **milliseconds**.
    """
    if credentials is None:
        return ("unknown", None)

    access_ms = credentials.get("expiresAt")
    refresh_ms = credentials.get("refreshTokenExpiresAt")

    if not _is_numeric(access_ms) or not _is_numeric(refresh_ms):
        return ("unknown", None)

    access_s = access_ms / 1000
    refresh_s = refresh_ms / 1000
    access_remaining = access_s - now
    refresh_remaining = refresh_s - now

    if refresh_remaining <= 0:
        return ("expired", int(min(access_remaining, refresh_remaining)))

    earliest_remaining = min(access_remaining, refresh_remaining)

    if earliest_remaining <= EXPIRING_WINDOW_SECONDS:
        return ("expiring", int(earliest_remaining))

    return ("valid", int(earliest_remaining))


class AuthResolution(NamedTuple):
    """The resolved global auth answer plus its provenance.

    ``expires_in_seconds`` is non-None only when ``source`` is
    ``"credentials_file"``: it is the sole source that carries
    timestamps. A ``"cli_probe"`` resolution deliberately reports None
    rather than a guessed window.
    """

    state: str                      # one of AUTH_STATES
    expires_in_seconds: int | None
    source: str                     # one of AUTH_SOURCES


def resolve_auth_state(
    credentials: Mapping[str, Any] | None,
    *,
    now: float,
    logged_in: bool | None = None,
) -> AuthResolution:
    """Combine the credentials-file classification with the CLI probe.

    ``credentials`` is the parsed credentials file (None when absent or
    unreadable). ``logged_in`` is the probe's tri-state answer —
    True/False from ``claude auth status``, None when the probe itself
    could not be run or parsed. It defaults to None so a caller with no
    probe wired behaves exactly as before.

    Precedence, and why:

    - File reads ``valid``/``expiring`` → the file wins outright. It has
      the timestamps, and the caller may skip the probe subprocess
      entirely on this (overwhelmingly common) path.
    - File reads ``expired`` → the probe gets a veto, because a stale
      ``.credentials.json`` left behind on a host that has since moved
      its tokens to the Keychain reads as long-expired forever. Believing
      it would pin a false "Login expired" badge on a healthy host and
      suppress the kick watchdog for good. A silent probe cannot veto:
      absence of evidence does not rescue an expired file.
    - File reads ``unknown`` → the probe is all there is. This is the
      keychain/gateway/relocated-config case the whole function exists
      for: ``loggedIn: false`` becomes a real ``"expired"`` the dashboard
      can act on instead of an ``"unknown"`` it used to swallow.
    - Neither source spoke → ``"unknown"`` with source ``"none"``. An
      unreadable signal stays unreadable (F16); it never becomes a claim.
    """
    file_state, file_expires_in = auth_state(credentials, now=now)

    if file_state in ("valid", "expiring"):
        return AuthResolution(file_state, file_expires_in, "credentials_file")

    if file_state == "expired":
        if logged_in is True:
            return AuthResolution("valid", None, "cli_probe")
        return AuthResolution("expired", file_expires_in, "credentials_file")

    # file_state == "unknown": the file told us nothing.
    if logged_in is True:
        return AuthResolution("valid", None, "cli_probe")
    if logged_in is False:
        return AuthResolution("expired", None, "cli_probe")
    return AuthResolution("unknown", None, "none")


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))
