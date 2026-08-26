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
