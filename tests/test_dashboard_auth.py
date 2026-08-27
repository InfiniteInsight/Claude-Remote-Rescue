"""Dashboard login auth primitives (spec 2026-08-26)."""

from __future__ import annotations

import secrets

import pytest

from crr.core import dashboard_auth


def test_hash_passphrase_produces_hex_strings():
    h, s = dashboard_auth.hash_passphrase("test-pass-phrase")
    assert isinstance(h, str) and len(h) > 0
    assert isinstance(s, str) and len(s) > 0
    bytes.fromhex(h)
    bytes.fromhex(s)


def test_verify_passphrase_accepts_correct():
    h, s = dashboard_auth.hash_passphrase("correct-horse")
    assert dashboard_auth.verify_passphrase("correct-horse", h, s) is True


def test_verify_passphrase_rejects_wrong():
    h, s = dashboard_auth.hash_passphrase("correct-horse")
    assert dashboard_auth.verify_passphrase("wrong-horse", h, s) is False


def test_hash_passphrase_rejects_too_short():
    with pytest.raises(dashboard_auth.PassphraseError):
        dashboard_auth.hash_passphrase("short")


def test_different_calls_produce_different_salts():
    _, s1 = dashboard_auth.hash_passphrase("same-pass-phrase")
    _, s2 = dashboard_auth.hash_passphrase("same-pass-phrase")
    assert s1 != s2


def test_token_round_trip():
    secret = secrets.token_bytes(32)
    token = dashboard_auth.create_token(secret, now=1000.0)
    assert dashboard_auth.validate_token(token, secret, max_age_seconds=3600, now=1500.0) is True


def test_token_expired():
    secret = secrets.token_bytes(32)
    token = dashboard_auth.create_token(secret, now=1000.0)
    assert dashboard_auth.validate_token(token, secret, max_age_seconds=3600, now=5000.0) is False


def test_token_invalid_after_secret_change():
    secret1 = secrets.token_bytes(32)
    secret2 = secrets.token_bytes(32)
    token = dashboard_auth.create_token(secret1, now=1000.0)
    assert dashboard_auth.validate_token(token, secret2, max_age_seconds=3600, now=1500.0) is False


def test_token_rejects_garbage():
    secret = secrets.token_bytes(32)
    assert dashboard_auth.validate_token("not-a-token", secret, max_age_seconds=3600) is False
    assert dashboard_auth.validate_token("", secret, max_age_seconds=3600) is False


def test_token_rejects_tampered_payload():
    import base64 as b64
    secret = secrets.token_bytes(32)
    token = dashboard_auth.create_token(secret, now=1000.0)
    parts = token.split(".")
    payload = bytearray(b64.urlsafe_b64decode(parts[0] + "=="))
    payload[0] ^= 0xFF
    parts[0] = b64.urlsafe_b64encode(bytes(payload)).rstrip(b"=").decode()
    tampered = ".".join(parts)
    assert dashboard_auth.validate_token(tampered, secret, max_age_seconds=3600, now=1500.0) is False


def test_store_default_state(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    assert store.login_enabled() is False
    assert store.bootstrap_dismissed() is False
    assert store.signing_secret() is None


def test_store_enable_sets_login_enabled(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    assert store.login_enabled() is True
    assert store.signing_secret() is not None


def test_store_enable_rejects_mismatched_confirm(tmp_path):
    import pytest
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    with pytest.raises(dashboard_auth.PassphraseError, match="do not match"):
        store.enable("my-passphrase", "different")


def test_store_enable_rejects_short_passphrase(tmp_path):
    import pytest
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    with pytest.raises(dashboard_auth.PassphraseError):
        store.enable("short", "short")


def test_store_verify_correct(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    assert store.verify("my-passphrase") is True


def test_store_verify_wrong(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    assert store.verify("wrong-passphrase") is False


def test_store_change_passphrase_invalidates_old_secret(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("old-passphrase", "old-passphrase")
    old_secret = store.signing_secret()
    store.change("old-passphrase", "new-passphrase!", "new-passphrase!")
    assert store.signing_secret() != old_secret
    assert store.verify("new-passphrase!") is True
    assert store.verify("old-passphrase") is False


def test_store_change_rejects_wrong_current(tmp_path):
    import pytest
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    with pytest.raises(dashboard_auth.PassphraseError, match="incorrect"):
        store.change("wrong", "new-phrase!", "new-phrase!")


def test_store_disable(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    store.disable("my-passphrase")
    assert store.login_enabled() is False


def test_store_disable_transitions_to_opted_out_not_undecided(tmp_path):
    """State-machine fix (spec 2026-08-26 revision): disable() must ALSO set
    bootstrap_dismissed=True — disabling IS opting out. Without this, the
    state right after an authenticated disable would be login_enabled=False
    AND bootstrap_dismissed=False, which is UNDECIDED — the exact state the
    blocking setup gate activates for. A user who just authenticated to
    turn login off must not be immediately re-blocked by that gate on their
    very next request."""
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    store.disable("my-passphrase")
    assert store.login_enabled() is False
    assert store.bootstrap_dismissed() is True


def test_store_disable_rejects_wrong_passphrase(tmp_path):
    import pytest
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    with pytest.raises(dashboard_auth.PassphraseError, match="incorrect"):
        store.disable("wrong")


def test_store_dismiss_bootstrap(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    assert store.bootstrap_dismissed() is False
    store.dismiss_bootstrap()
    assert store.bootstrap_dismissed() is True


def test_store_absent_file_is_not_corrupt(tmp_path):
    """State 1 (fresh install / upgrade path): no file at all must never
    read as corrupt — that would make the live gate (login_enabled() OR
    is_corrupt()) active for every user who never configured login."""
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    assert store.is_corrupt() is False
    assert store.login_enabled() is False
    assert store.bootstrap_dismissed() is False


def test_store_valid_file_is_not_corrupt(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    assert store.is_corrupt() is False


def test_store_corrupt_invalid_json_fails_closed(tmp_path):
    """State 3: file present but not valid JSON. `login_enabled()` stays
    False (accessor semantics unchanged — `_read()` still returns {} for
    both absent and corrupt), but `is_corrupt()` now distinguishes this
    from state 1 so the CLI wiring can fail the auth gate CLOSED instead
    of treating it as login-never-configured."""
    (tmp_path / dashboard_auth.FILENAME).write_text("not json")
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    assert store.is_corrupt() is True
    assert store.login_enabled() is False
    assert store.bootstrap_dismissed() is False
    assert store.signing_secret() is None


def test_store_corrupt_not_a_dict_fails_closed(tmp_path):
    (tmp_path / dashboard_auth.FILENAME).write_text("[1, 2, 3]")
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    assert store.is_corrupt() is True


def test_store_corrupt_future_version_fails_closed(tmp_path):
    import json
    (tmp_path / dashboard_auth.FILENAME).write_text(
        json.dumps({"v": 999, "login_enabled": True})
    )
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    assert store.is_corrupt() is True


def test_store_corrupt_unreadable_file_fails_closed(tmp_path):
    """An OSError other than FileNotFoundError (e.g. permission denied) on
    a path that exists but can't be read as a file is treated as corrupt,
    not absent — fail closed is the safe direction for an unreadable auth
    store. A directory at the expected path raises IsADirectoryError /
    PermissionError on every OS and euid (unlike chmod(0), which root and
    Windows both ignore), so it's used here instead."""
    (tmp_path / dashboard_auth.FILENAME).mkdir()
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    assert store.is_corrupt() is True


def test_store_enable_rejects_when_already_enabled(tmp_path):
    """An authenticated-but-not-passphrase-holding client (e.g. stolen
    cookie) must not be able to overwrite hash/salt/secret via "enable" —
    that would lock the real owner out without ever proving they knew the
    current passphrase. "change" is the only path once login is on."""
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    with pytest.raises(dashboard_auth.PassphraseError, match="already enabled"):
        store.enable("new-passphrase", "new-passphrase")
    # The original passphrase and secret must be untouched.
    assert store.verify("my-passphrase") is True


def test_store_re_enable_generates_new_secret(tmp_path):
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    store.enable("my-passphrase", "my-passphrase")
    secret1 = store.signing_secret()
    store.disable("my-passphrase")
    store.enable("new-pass-phrase", "new-pass-phrase")
    assert store.signing_secret() != secret1


def test_rate_limiter_permits_first_five():
    rl = dashboard_auth.LoginRateLimiter()
    for _ in range(5):
        assert rl.check() == 0.0
        rl.record_failure()


def test_rate_limiter_blocks_after_five():
    rl = dashboard_auth.LoginRateLimiter()
    for _ in range(5):
        rl.record_failure()
    delay = rl.check()
    assert delay > 0


def test_rate_limiter_delay_grows_exponentially():
    rl = dashboard_auth.LoginRateLimiter()
    for _ in range(6):
        rl.record_failure()
    d1 = rl.check()
    rl.record_failure()
    d2 = rl.check()
    assert d2 > d1


def test_rate_limiter_caps_at_300():
    rl = dashboard_auth.LoginRateLimiter()
    for _ in range(50):
        rl.record_failure()
    assert rl.check() <= 300


def test_rate_limiter_resets_on_success():
    rl = dashboard_auth.LoginRateLimiter()
    for _ in range(10):
        rl.record_failure()
    rl.reset()
    assert rl.check() == 0.0


def test_login_page_returns_html():
    html = dashboard_auth.login_page()
    assert "<form" in html
    assert "passphrase" in html.lower()
    assert "<title>" in html


def test_login_page_includes_error_message():
    html = dashboard_auth.login_page(error="bad password")
    assert "bad password" in html


def test_login_page_no_error_by_default():
    html = dashboard_auth.login_page()
    assert 'id="error"' in html or "error" not in html.lower().split("<form")[0]


def test_login_page_escapes_error_html():
    html = dashboard_auth.login_page(error="<script>x</script>")
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_setup_page_has_no_dashboard_content():
    """The blocking setup page (default-secure bootstrap, spec 2026-08-26
    revision) must be self-contained like login_page() — zero dashboard
    content, so an unauthenticated UNDECIDED visitor never sees session
    data or dashboard chrome."""
    html = dashboard_auth.setup_page()
    assert "Secure this dashboard" in html
    assert 'id="sessions"' not in html
    assert "Claude-Remote-Rescue" not in html  # the dashboard's own <title>


def test_setup_page_offers_enable_and_dismiss():
    html = dashboard_auth.setup_page()
    assert "enable" in html.lower()
    assert "passphrase" in html.lower()
    assert "confirm" in html.lower()
    assert "dashboard-auth" in html
    assert "dismiss-bootstrap" in html
    assert "Continue without login" in html


def test_setup_page_is_distinct_from_login_page():
    setup_html = dashboard_auth.setup_page()
    login_html = dashboard_auth.login_page()
    assert setup_html != login_html
    assert "Log in" not in setup_html
    assert "Secure this dashboard" not in login_html
