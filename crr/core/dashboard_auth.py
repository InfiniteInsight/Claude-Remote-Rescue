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


class DashboardAuthStore:
    """Read/write the dashboard login auth file."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / FILENAME

    def _read(self) -> dict[str, Any]:
        try:
            data = read_json_file(self._path)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        if not contracts.store_version_ok(data, contracts.DASHBOARD_AUTH_STORE_VERSION):
            return {}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data["v"] = contracts.DASHBOARD_AUTH_STORE_VERSION
        write_json_atomic(self._path, data)

    def login_enabled(self) -> bool:
        return bool(self._read().get("login_enabled", False))

    def bootstrap_dismissed(self) -> bool:
        return bool(self._read().get("bootstrap_dismissed", False))

    def signing_secret(self) -> bytes | None:
        raw = self._read().get("signing_secret")
        if isinstance(raw, str):
            try:
                return bytes.fromhex(raw)
            except ValueError:
                return None
        return None

    def verify(self, passphrase: str) -> bool:
        data = self._read()
        h = data.get("passphrase_hash")
        s = data.get("passphrase_salt")
        if not isinstance(h, str) or not isinstance(s, str):
            return False
        return verify_passphrase(passphrase, h, s)

    def enable(self, passphrase: str, confirm: str) -> None:
        if passphrase != confirm:
            raise PassphraseError("passphrases do not match")
        h, s = hash_passphrase(passphrase)
        self._write({
            "login_enabled": True,
            "bootstrap_dismissed": False,
            "passphrase_hash": h,
            "passphrase_salt": s,
            "signing_secret": secrets.token_bytes(32).hex(),
        })

    def change(self, current: str, new_passphrase: str, confirm: str) -> None:
        if not self.verify(current):
            raise PassphraseError("current passphrase is incorrect")
        if new_passphrase != confirm:
            raise PassphraseError("new passphrases do not match")
        h, s = hash_passphrase(new_passphrase)
        data = self._read()
        data.update({
            "passphrase_hash": h,
            "passphrase_salt": s,
            "signing_secret": secrets.token_bytes(32).hex(),
        })
        self._write(data)

    def disable(self, current: str) -> None:
        if not self.verify(current):
            raise PassphraseError("current passphrase is incorrect")
        data = self._read()
        data["login_enabled"] = False
        self._write(data)

    def dismiss_bootstrap(self) -> None:
        data = self._read()
        data["bootstrap_dismissed"] = True
        self._write(data)


class LoginRateLimiter:
    """Global in-memory login attempt rate limiter."""

    def __init__(self) -> None:
        self._failures = 0

    def check(self) -> float:
        """Seconds to wait before the next attempt is allowed. 0.0 = ok."""
        if self._failures < 5:
            return 0.0
        return min(2 ** (self._failures - 5), 300)

    def record_failure(self) -> None:
        self._failures += 1

    def reset(self) -> None:
        self._failures = 0
