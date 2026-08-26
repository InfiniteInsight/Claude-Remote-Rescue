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


def test_store_corrupt_file_degrades_to_disabled(tmp_path):
    (tmp_path / dashboard_auth.FILENAME).write_text("not json")
    store = dashboard_auth.DashboardAuthStore(tmp_path)
    assert store.login_enabled() is False
    assert store.bootstrap_dismissed() is False


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
