"""Auth state detection — pure core, no I/O.

Takes parsed credential data and a wall-clock timestamp, returns the
OAuth state and time until expiration. The file read happens in the CLI
layer; this module only decides.

Mirrors ``crr.core.reachability``: an adapter reads, core classifies.
"""

from __future__ import annotations

from typing import Any, Mapping

from crr.core.contracts import AUTH_STATES

__all__ = ["AUTH_STATES", "EXPIRING_WINDOW_SECONDS", "auth_state"]

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


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))
