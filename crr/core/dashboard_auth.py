"""Dashboard login — optional passphrase auth gate (spec 2026-08-26).

Pure core module. Handles passphrase hashing (scrypt), stateless HMAC-SHA256
session tokens, rate limiting, and the login page HTML. Wired by the CLI;
never imports adapters or cli.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from pathlib import Path
from typing import Any

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic

MIN_PASSPHRASE_LENGTH = 8
COOKIE_NAME = "crr_session"
FILENAME = "dashboard_auth.json"


class PassphraseError(ValueError):
    """A rejected passphrase (too short, mismatch, etc.)."""


def hash_passphrase(passphrase: str) -> tuple[str, str]:
    """Hash a passphrase with scrypt. Returns (hash_hex, salt_hex)."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise PassphraseError(
            f"passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters"
        )
    salt = os.urandom(16)
    h = hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return h.hex(), salt.hex()


def verify_passphrase(passphrase: str, hash_hex: str, salt_hex: str) -> bool:
    """Verify a passphrase against a stored hash. Timing-safe."""
    h = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=2**14, r=8, p=1, dklen=32,
    )
    return hmac.compare_digest(h, bytes.fromhex(hash_hex))


def create_token(signing_secret: bytes, now: float | None = None) -> str:
    """Create a stateless HMAC-SHA256 session token."""
    ts = now if now is not None else time.time()
    token_id = secrets.token_bytes(16)
    payload = token_id + struct.pack(">d", ts)
    sig = hmac.new(signing_secret, payload, "sha256").digest()
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{payload_b64}.{sig_b64}"


def validate_token(
    token: str, signing_secret: bytes, max_age_seconds: float,
    now: float | None = None,
) -> bool:
    """Validate a stateless HMAC-SHA256 session token. Timing-safe."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return False
        payload = base64.urlsafe_b64decode(parts[0] + "==")
        sig = base64.urlsafe_b64decode(parts[1] + "==")
        if len(payload) != 24:  # 16 (token_id) + 8 (double)
            return False
        expected_sig = hmac.new(signing_secret, payload, "sha256").digest()
        if not hmac.compare_digest(sig, expected_sig):
            return False
        (issued_at,) = struct.unpack(">d", payload[16:])
        ts = now if now is not None else time.time()
        return (ts - issued_at) < max_age_seconds
    except Exception:
        return False
