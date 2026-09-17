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


class TestResolveAuthState:
    """The two-source resolver (spec 2026-09-17, keychain-blind reauth).

    ``auth_state`` above classifies the credentials FILE. It cannot see a
    host that keeps its OAuth tokens somewhere else (macOS Keychain, a
    relocated ``CLAUDE_CONFIG_DIR``, an enterprise gateway), and on those
    hosts it can only ever answer "unknown" — which the dashboard used to
    render identically to "valid", leaving no badge and no Reauth button
    at exactly the moment the user needed one.

    ``resolve_auth_state`` adds the second source: the logged-in answer
    that ``crr.adapters.claude_auth`` scrapes out of ``claude auth
    status --json``. It is coarser (no timestamps, so no advance
    "expiring" warning) but it is authoritative about the one question
    that matters when the file is absent. ``source`` carries which one
    spoke (P3 — confidence travels with data).
    """

    def test_file_valid_wins_without_consulting_the_probe(self):
        # The file has real timestamps; the probe has none. When the file
        # is readable and healthy it is the better source, and the caller
        # is entitled to skip the subprocess entirely.
        creds = _creds(access_expires_s=10 * 86400, refresh_expires_s=_30D_MS / 1000)
        res = auth.resolve_auth_state(creds, now=_NOW, logged_in=None)
        assert res.state == "valid"
        assert res.source == "credentials_file"
        assert res.expires_in_seconds is not None

    def test_file_expiring_keeps_its_countdown(self):
        creds = _creds(access_expires_s=2 * 86400, refresh_expires_s=28 * 86400)
        res = auth.resolve_auth_state(creds, now=_NOW, logged_in=None)
        assert res.state == "expiring"
        assert res.source == "credentials_file"
        assert res.expires_in_seconds == pytest.approx(2 * 86400, abs=1)

    def test_no_file_and_probe_says_logged_out_is_expired(self):
        # THE BUG THIS EXISTS FOR. No credentials file, so the old code
        # said "unknown" and the dashboard drew nothing. The probe knows.
        res = auth.resolve_auth_state(None, now=_NOW, logged_in=False)
        assert res.state == "expired"
        assert res.source == "cli_probe"
        # The probe carries no timestamps, so there is no countdown to
        # report. A fabricated one would be exactly the laundering the
        # plumb-line principles forbid.
        assert res.expires_in_seconds is None

    def test_no_file_and_probe_says_logged_in_is_valid(self):
        # A Mac keeping its tokens in the Keychain: no file, fully logged
        # in. Must NOT nag with a spurious "login expired" badge.
        res = auth.resolve_auth_state(None, now=_NOW, logged_in=True)
        assert res.state == "valid"
        assert res.source == "cli_probe"
        assert res.expires_in_seconds is None

    def test_no_file_and_no_probe_stays_unknown(self):
        # Both sources silent. Honest null — never promoted to either
        # confident answer (F16).
        res = auth.resolve_auth_state(None, now=_NOW, logged_in=None)
        assert res.state == "unknown"
        assert res.source == "none"
        assert res.expires_in_seconds is None

    def test_malformed_file_falls_through_to_the_probe(self):
        res = auth.resolve_auth_state({"expiresAt": "soon"}, now=_NOW, logged_in=False)
        assert res.state == "expired"
        assert res.source == "cli_probe"

    def test_stale_expired_file_is_overridden_by_a_live_probe(self):
        # A leftover .credentials.json from before the host moved to the
        # Keychain reads as long-expired. Believing it would show a
        # permanent "Login expired" badge on a perfectly healthy host and
        # suppress the kick watchdog forever.
        creds = _creds(access_expires_s=-_30D_MS / 1000, refresh_expires_s=-_30D_MS / 1000)
        res = auth.resolve_auth_state(creds, now=_NOW, logged_in=True)
        assert res.state == "valid"
        assert res.source == "cli_probe"

    def test_expired_file_stands_when_the_probe_agrees(self):
        creds = _creds(access_expires_s=-86400, refresh_expires_s=-86400)
        res = auth.resolve_auth_state(creds, now=_NOW, logged_in=False)
        assert res.state == "expired"
        assert res.source == "credentials_file"
        # The file's countdown survives — it is the more informative of
        # two sources that agree.
        assert res.expires_in_seconds is not None
        assert res.expires_in_seconds < 0

    def test_expired_file_stands_when_the_probe_is_silent(self):
        # An unreadable probe must not rescue a genuinely expired file.
        creds = _creds(access_expires_s=-86400, refresh_expires_s=-86400)
        res = auth.resolve_auth_state(creds, now=_NOW, logged_in=None)
        assert res.state == "expired"
        assert res.source == "credentials_file"

    def test_every_resolved_state_is_a_declared_member(self):
        from crr.core.contracts import AUTH_SOURCES, AUTH_STATES
        creds = _creds(access_expires_s=86400, refresh_expires_s=10 * 86400)
        for candidate in (None, {}, {"expiresAt": "x"}, creds):
            for logged_in in (True, False, None):
                res = auth.resolve_auth_state(candidate, now=_NOW, logged_in=logged_in)
                assert res.state in AUTH_STATES
                assert res.source in AUTH_SOURCES

    def test_logged_in_defaults_to_silent(self):
        # Callers that have no probe wired must not have to say so.
        assert auth.resolve_auth_state(None, now=_NOW).state == "unknown"
