"""Auth state detection — pure, no file I/O."""

from __future__ import annotations

import pytest

from crr.core import auth


# A fresh epoch reference (arbitrary). Tests express offsets from this.
_NOW = 1_700_000_000.0  # seconds

# 12 hours in ms (access token lifetime observed empirically)
_12H_MS = 12 * 3600 * 1000
# 30 days in ms (refresh token lifetime observed empirically)
_30D_MS = 30 * 24 * 3600 * 1000
# 3 days in seconds (the warning window)
_3D_S = 3 * 24 * 3600


def _creds(*, access_expires_s: float, refresh_expires_s: float) -> dict:
    """Build a minimal credentials dict with expiration offsets from _NOW.

    Positive offset = expires in the future. Negative = already expired.
    Timestamps are Unix epoch MILLISECONDS (Claude Code's format).
    """
    return {
        "expiresAt": int((_NOW + access_expires_s) * 1000),
        "refreshTokenExpiresAt": int((_NOW + refresh_expires_s) * 1000),
    }


class TestAuthState:
    def test_valid_both_tokens_fresh(self):
        creds = _creds(access_expires_s=4 * 86400, refresh_expires_s=_30D_MS / 1000)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "valid"
        assert expires_in is not None
        assert expires_in > _3D_S

    def test_expiring_within_3_days(self):
        # Access token expires in 2 days, refresh still has 28 days
        creds = _creds(access_expires_s=2 * 86400, refresh_expires_s=28 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expiring"
        assert expires_in == pytest.approx(2 * 86400, abs=1)

    def test_expiring_boundary_exactly_3_days(self):
        # At exactly 3 days, state is EXPIRING (inclusive boundary)
        creds = _creds(access_expires_s=_3D_S, refresh_expires_s=28 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expiring"

    def test_valid_boundary_just_over_3_days(self):
        # 3 days + 1 second is VALID
        creds = _creds(access_expires_s=_3D_S + 1, refresh_expires_s=28 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "valid"

    def test_expired_refresh_token_gone(self):
        # Refresh token expired, access token still alive
        creds = _creds(access_expires_s=3600, refresh_expires_s=-3600)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expired"

    def test_expired_both_tokens_gone(self):
        creds = _creds(access_expires_s=-3600, refresh_expires_s=-7200)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expired"

    def test_expiring_access_expired_refresh_alive(self):
        # Access expired but refresh still valid and within 3 days — EXPIRING
        # (a kick/restart will trigger doRefresh)
        creds = _creds(access_expires_s=-3600, refresh_expires_s=2 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expiring"

    def test_expiring_access_expired_refresh_alive_far_future(self):
        # Access expired, refresh valid but far beyond the 3-day window
        # — still "expiring" because access-expired is recoverable
        creds = _creds(access_expires_s=-3600, refresh_expires_s=25 * 86400)
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "expiring"

    def test_unknown_none_credentials(self):
        state, expires_in = auth.auth_state(None, now=_NOW)
        assert state == "unknown"
        assert expires_in is None

    def test_unknown_empty_dict(self):
        state, expires_in = auth.auth_state({}, now=_NOW)
        assert state == "unknown"
        assert expires_in is None

    def test_unknown_missing_refresh_field(self):
        state, expires_in = auth.auth_state({"expiresAt": 9999999999999}, now=_NOW)
        assert state == "unknown"
        assert expires_in is None

    def test_unknown_non_numeric_values(self):
        state, expires_in = auth.auth_state(
            {"expiresAt": "tomorrow", "refreshTokenExpiresAt": "next week"}, now=_NOW
        )
        assert state == "unknown"
        assert expires_in is None

    def test_unknown_boolean_values_rejected(self):
        # bool is a subclass of int — must be rejected
        state, expires_in = auth.auth_state(
            {"expiresAt": True, "refreshTokenExpiresAt": False}, now=_NOW
        )
        assert state == "unknown"
        assert expires_in is None

    def test_millisecond_precision(self):
        # Timestamps are ms; ensure no off-by-1000 errors
        # Use 4 days (> 3-day window) so the state is "valid"
        offset_s = 4 * 86400
        ms = int((_NOW + offset_s) * 1000)
        creds = {"expiresAt": ms, "refreshTokenExpiresAt": ms + _30D_MS}
        state, expires_in = auth.auth_state(creds, now=_NOW)
        assert state == "valid"
        # expires_in should be ~offset_s, not ~offset_s*1000
        assert offset_s - 1 <= expires_in <= offset_s + 1

    def test_expires_in_is_smallest_positive(self):
        # Access in 1 day, refresh in 10 days — expires_in is 1 day
        creds = _creds(access_expires_s=86400, refresh_expires_s=10 * 86400)
        _, expires_in = auth.auth_state(creds, now=_NOW)
        assert expires_in == pytest.approx(86400, abs=1)


class TestAuthStateConstants:
    def test_enum_tuple(self):
        # AUTH_STATES is defined in contracts.py (per project convention:
        # all shared enums in one place). auth.py re-exports it.
        from crr.core.contracts import AUTH_STATES
        assert AUTH_STATES == ("valid", "expiring", "expired", "unknown")
        assert auth.AUTH_STATES is AUTH_STATES  # re-export, not a copy

    def test_window_is_3_days(self):
        assert auth.EXPIRING_WINDOW_SECONDS == 3 * 24 * 3600
