# Dashboard Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional passphrase-based login to the CRR dashboard so it works as a real security boundary independent of the network layer.

**Architecture:** A new pure-core module `crr/core/dashboard_auth.py` handles passphrase hashing (scrypt), stateless HMAC-SHA256 session tokens, rate limiting, and the login page HTML. The web handler (`web.py`) gains an auth middleware layer that checks cookies before routing. The CLI wires a `DashboardAuthStore` into the handler. `page.html` gains a bootstrap prompt modal and a Settings-modal login section.

**Tech Stack:** Python 3.12 stdlib only — `hashlib.scrypt`, `hmac`, `secrets`, `base64`, `time`, `json`.

## Global Constraints

- Zero runtime dependencies (stdlib only).
- One-way layering: `crr.cli` → `crr.adapters` → `crr.core`. `dashboard_auth.py` lives in `crr.core`.
- TDD: tests first, implementation second.
- `PAGE_VERSION` must bump for every `page.html` change.
- `CONFIG_DEFAULTS_VERSION` must bump for every new config key, with a `# vN ...` ledger comment.
- `textContent` for untrusted fields in page.html.
- Version ledger: every version bump needs a `# vN ...` comment in the comment block above the constant. `tests/test_version_ledger.py` fails on a hole.
- `DASHBOARD_AUTH_STORE_VERSION = 1` in `contracts.py` — add it next to the existing store versions.
- The login page is a self-contained HTML string returned by `dashboard_auth.login_page()`, NOT a template in `page.html`.
- Cookie: `crr_session=<token>; HttpOnly; SameSite=Strict; Path=/; Max-Age=<seconds>`. NO `Secure` flag (CRR serves plain HTTP on loopback).

---

### Task 1: Pure auth primitives — passphrase hashing and token creation/validation

**Files:**
- Create: `crr/core/dashboard_auth.py`
- Create: `tests/test_dashboard_auth.py`
- Modify: `crr/core/contracts.py:99-101` (add `DASHBOARD_AUTH_STORE_VERSION = 1`)

**Interfaces:**
- Consumes: `crr.core.journal.read_json_file`, `crr.core.journal.write_json_atomic`, `crr.core.contracts.store_version_ok`
- Produces (used by Tasks 2-5):
  - `hash_passphrase(passphrase: str) -> tuple[str, str]` — returns `(hash_hex, salt_hex)`
  - `verify_passphrase(passphrase: str, hash_hex: str, salt_hex: str) -> bool` — timing-safe
  - `create_token(signing_secret: bytes, now: float | None = None) -> str` — stateless HMAC token
  - `validate_token(token: str, signing_secret: bytes, max_age_seconds: float, now: float | None = None) -> bool`
  - `MIN_PASSPHRASE_LENGTH = 8`
  - `PassphraseError(ValueError)` — raised on too-short passphrase or mismatched confirm
  - `COOKIE_NAME = "crr_session"`

- [ ] **Step 1: Write the contract constant**

Add to `crr/core/contracts.py` immediately after line 101 (`KICKS_STORE_VERSION = 1`):

```python
DASHBOARD_AUTH_STORE_VERSION = 1
```

- [ ] **Step 2: Write tests for passphrase hashing**

Create `tests/test_dashboard_auth.py`:

```python
"""Dashboard login auth primitives (spec 2026-08-26)."""

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
    import pytest
    with pytest.raises(dashboard_auth.PassphraseError):
        dashboard_auth.hash_passphrase("short")


def test_different_calls_produce_different_salts():
    _, s1 = dashboard_auth.hash_passphrase("same-pass-phrase")
    _, s2 = dashboard_auth.hash_passphrase("same-pass-phrase")
    assert s1 != s2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v`
Expected: FAIL (module not found)

- [ ] **Step 4: Implement passphrase hashing**

Create `crr/core/dashboard_auth.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v`
Expected: PASS

- [ ] **Step 6: Write tests for token creation/validation**

Append to `tests/test_dashboard_auth.py`:

```python
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
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v -k token`
Expected: FAIL (create_token not defined)

- [ ] **Step 8: Implement token creation/validation**

Append to `crr/core/dashboard_auth.py`:

```python
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
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add crr/core/contracts.py crr/core/dashboard_auth.py tests/test_dashboard_auth.py
git commit -m "feat(auth): add passphrase hashing and stateless HMAC token primitives"
```

---

### Task 2: Auth store and rate limiter

**Files:**
- Modify: `crr/core/dashboard_auth.py`
- Modify: `tests/test_dashboard_auth.py`

**Interfaces:**
- Consumes: `hash_passphrase`, `verify_passphrase`, `create_token`, `validate_token` from Task 1
- Produces (used by Tasks 3-5):
  - `DashboardAuthStore(state_dir: Path)` with methods:
    - `.login_enabled() -> bool`
    - `.bootstrap_dismissed() -> bool`
    - `.signing_secret() -> bytes | None`
    - `.verify(passphrase: str) -> bool`
    - `.enable(passphrase: str, confirm: str) -> None` — raises `PassphraseError` on mismatch or too short
    - `.change(current: str, new_passphrase: str, confirm: str) -> None`
    - `.disable(current: str) -> None`
    - `.dismiss_bootstrap() -> None`
  - `LoginRateLimiter()` with methods:
    - `.check() -> float` — returns seconds to wait (0.0 if ok)
    - `.record_failure() -> None`
    - `.reset() -> None`

- [ ] **Step 1: Write tests for the auth store**

Append to `tests/test_dashboard_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v -k store`
Expected: FAIL (DashboardAuthStore not defined)

- [ ] **Step 3: Implement DashboardAuthStore**

Append to `crr/core/dashboard_auth.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v`
Expected: PASS

- [ ] **Step 5: Write tests for rate limiter**

Append to `tests/test_dashboard_auth.py`:

```python
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
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v -k rate`
Expected: FAIL (LoginRateLimiter not defined)

- [ ] **Step 7: Implement LoginRateLimiter**

Append to `crr/core/dashboard_auth.py`:

```python
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
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add crr/core/dashboard_auth.py tests/test_dashboard_auth.py
git commit -m "feat(auth): add DashboardAuthStore and LoginRateLimiter"
```

---

### Task 3: Login page HTML and config key

**Files:**
- Modify: `crr/core/dashboard_auth.py`
- Modify: `crr/core/config.py:102,302` (bump `CONFIG_DEFAULTS_VERSION`, add `dashboard_session_hours`)
- Modify: `tests/test_dashboard_auth.py`
- Modify: `tests/test_version_ledger.py` (no change needed if ledger comment is correct)

**Interfaces:**
- Consumes: nothing new
- Produces (used by Tasks 4-5):
  - `login_page(error: str = "") -> str` — self-contained HTML login page
  - `dashboard_session_hours` config key (default 720)

- [ ] **Step 1: Write test for login_page**

Append to `tests/test_dashboard_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v -k login_page`
Expected: FAIL (login_page not defined)

- [ ] **Step 3: Implement login_page**

Append to `crr/core/dashboard_auth.py`:

```python
def login_page(error: str = "") -> str:
    """Self-contained HTML login page. Never includes dashboard content."""
    error_html = (
        f'<p id="error" style="color:#fca5a5; margin:0 0 12px;">'
        f'{error}</p>'
        if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crr — login</title>
<style>
  body {{ margin:0; background:#0d1117; color:#cdd3dd; font-family:system-ui, -apple-system, sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; }}
  .box {{ background:#12161d; border:1px solid #2f3745; border-radius:8px; padding:28px; max-width:360px; width:90%; }}
  h1 {{ font-size:18px; margin:0 0 16px; color:#e6e6e6; }}
  label {{ display:block; font-size:13px; margin:0 0 6px; color:#8a93a2; }}
  input[type=password] {{ width:100%; padding:8px 10px; box-sizing:border-box; background:#1c222c; color:#cdd3dd; border:1px solid #2f3745; border-radius:6px; font:inherit; font-size:14px; }}
  button {{ margin-top:14px; width:100%; padding:8px; background:#2563eb; color:#fff; border:none; border-radius:6px; font:inherit; font-size:14px; cursor:pointer; }}
  button:hover {{ background:#1d4ed8; }}
</style>
</head>
<body>
<div class="box">
  <h1>crr dashboard</h1>
  {error_html}
  <form method="POST" action="/api/login" id="loginForm">
    <label for="pp">Passphrase</label>
    <input type="password" id="pp" name="passphrase" autofocus required>
    <button type="submit">Log in</button>
  </form>
  <script>
    document.getElementById('loginForm').addEventListener('submit', function(e) {{
      e.preventDefault();
      var pp = document.getElementById('pp').value;
      fetch('/api/login', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{passphrase: pp}})
      }}).then(function(r) {{
        if (r.ok) location.href = '/';
        else return r.json();
      }}).then(function(d) {{
        if (d && d.error) {{
          var el = document.getElementById('error');
          if (!el) {{
            el = document.createElement('p');
            el.id = 'error';
            el.style.cssText = 'color:#fca5a5; margin:0 0 12px;';
            document.querySelector('.box').insertBefore(el, document.getElementById('loginForm'));
          }}
          el.textContent = d.error;
        }}
      }});
    }});
  </script>
</div>
</body>
</html>"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dashboard_auth.py -v -k login_page`
Expected: PASS

- [ ] **Step 5: Add config key**

In `crr/core/config.py`, add the ledger comment before `CONFIG_DEFAULTS_VERSION` and bump it:

```python
# v23: added dashboard_session_hours (dashboard login — optional passphrase
# auth gate; see crr.core.dashboard_auth)
CONFIG_DEFAULTS_VERSION = 23
```

Add the key to `DEFAULTS` dict (after `reauth_success_display_ms`):

```python
    # dashboard login (spec 2026-08-26): how long a login session cookie
    # stays valid. 720 hours = 30 days. Changing this does not invalidate
    # existing sessions — it only affects the Max-Age on NEW cookies and
    # the server-side expiry check.
    "dashboard_session_hours": 720,
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (including version ledger tests)

- [ ] **Step 7: Commit**

```bash
git add crr/core/dashboard_auth.py crr/core/config.py tests/test_dashboard_auth.py
git commit -m "feat(auth): add login page HTML and dashboard_session_hours config key"
```

---

### Task 4: Web handler auth middleware + login/logout/dashboard-auth endpoints

**Files:**
- Modify: `crr/core/web.py` (auth layer in `handle_request`, new endpoints)
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes:
  - `dashboard_auth.login_page(error: str) -> str` (Task 3)
  - `dashboard_auth.validate_token(token, secret, max_age) -> bool` (Task 1)
  - `dashboard_auth.create_token(secret) -> str` (Task 1)
  - `dashboard_auth.COOKIE_NAME` (Task 1)
- Produces (used by Task 5):
  - `handle_request` gains new keyword args:
    - `auth_enabled: bool = False`
    - `auth_check: Callable[[str], bool] | None = None` — given cookie value, returns True if valid
    - `login_provider: Callable[[str], tuple[bool, str, dict[str, str]]] | None = None` — (ok, error_or_empty, extra_headers)
    - `logout_provider: Callable[[], dict[str, str]] | None = None` — returns extra headers (Set-Cookie clear)
    - `dashboard_auth_provider: Callable[[dict], tuple[bool, str]] | None = None` — settings modal ops
    - `bootstrap_state: dict | None = None` — `{"login_enabled": bool, "bootstrap_dismissed": bool}` injected into sessions payload

- [ ] **Step 1: Write tests for auth middleware — unauthenticated blocked**

Append to `tests/test_web.py`. The web tests already import `web` and use `handle_request` with a `sessions_provider` and `allowed_hosts`. Follow the existing pattern:

```python
def test_auth_enabled_blocks_unauthenticated_api(provider, hosts):
    resp = web.handle_request(
        "GET", "/api/sessions", {"Host": "localhost"},
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=True,
        auth_check=lambda cookie: False,
    )
    assert resp.status == 401
    assert b"unauthorized" in resp.body


def test_auth_enabled_serves_login_page_for_root(provider, hosts):
    resp = web.handle_request(
        "GET", "/", {"Host": "localhost"},
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=True,
        auth_check=lambda cookie: False,
    )
    assert resp.status == 200
    assert b"passphrase" in resp.body.lower()
    assert b"login" in resp.body.lower()


def test_auth_enabled_allows_version_without_cookie(provider, hosts):
    resp = web.handle_request(
        "GET", "/api/version", {"Host": "localhost"},
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=True,
        auth_check=lambda cookie: False,
    )
    assert resp.status == 200


def test_auth_enabled_allows_manifest_without_cookie(provider, hosts):
    resp = web.handle_request(
        "GET", "/manifest.webmanifest", {"Host": "localhost"},
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=True,
        auth_check=lambda cookie: False,
    )
    assert resp.status == 200


def test_auth_enabled_passes_with_valid_cookie(provider, hosts):
    resp = web.handle_request(
        "GET", "/api/sessions", {"Host": "localhost", "Cookie": "crr_session=valid-token"},
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=True,
        auth_check=lambda cookie: cookie == "valid-token",
    )
    assert resp.status == 200


def test_auth_disabled_passes_without_cookie(provider, hosts):
    resp = web.handle_request(
        "GET", "/api/sessions", {"Host": "localhost"},
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=False,
    )
    assert resp.status == 200
```

Note: `provider` and `hosts` should use whatever fixtures `test_web.py` already uses for its existing tests. Read the file to find the exact fixture names and replicate their usage pattern.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -v -k auth_enabled`
Expected: FAIL (unexpected keyword argument 'auth_enabled')

- [ ] **Step 3: Implement auth middleware in handle_request**

In `crr/core/web.py`, add the new keyword args to `handle_request`'s signature and add the auth check after the host allowlist but before routing:

```python
def handle_request(
    method, path, headers, body=b"",
    *,
    # ... existing params ...
    auth_enabled: bool = False,
    auth_check: Callable[[str], bool] | None = None,
    login_provider: Callable[[str], tuple[bool, str, dict[str, str]]] | None = None,
    logout_provider: Callable[[], dict[str, str]] | None = None,
    dashboard_auth_provider: Callable[[dict], tuple[bool, str]] | None = None,
    bootstrap_state: dict | None = None,
) -> Response:
```

After the host allowlist check, before the `if method == "GET":` block:

```python
    # Auth gate (spec 2026-08-26): when login is enabled, every request
    # except the exemptions below must carry a valid session cookie.
    if auth_enabled and auth_check is not None:
        _AUTH_EXEMPT = {
            "/api/version", "/manifest.webmanifest", "/sw.js",
            "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png",
        }
        _AUTH_EXEMPT_POST = {"/api/login"}
        exempt = (method == "GET" and path in _AUTH_EXEMPT) or \
                 (method == "POST" and path in _AUTH_EXEMPT_POST)
        if not exempt:
            cookie_val = _parse_cookie(_header(headers, "Cookie"), dashboard_auth.COOKIE_NAME)
            if not cookie_val or not auth_check(cookie_val):
                if method == "GET" and path == "/":
                    from crr.core import dashboard_auth as _da
                    page = _da.login_page()
                    return _resp(200, "text/html; charset=utf-8", page.encode("utf-8"))
                return _json(401, {"error": "unauthorized"})
```

Add the `_parse_cookie` helper (before `handle_request`):

```python
def _parse_cookie(cookie_header: str, name: str) -> str:
    """Extract a named cookie value from a Cookie header. '' if absent."""
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part[len(name) + 1:]
    return ""
```

Import `dashboard_auth` at the top of `web.py`:

```python
from crr.core import dashboard_auth
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -v -k auth_enabled`
Expected: PASS

- [ ] **Step 5: Write tests for login/logout endpoints**

Append to `tests/test_web.py`:

```python
def test_login_endpoint_success_sets_cookie(provider, hosts):
    import json
    def login_prov(passphrase):
        if passphrase == "correct":
            return True, "", {"Set-Cookie": "crr_session=tok123; HttpOnly; SameSite=Strict; Path=/"}
        return False, "Incorrect passphrase", {}

    resp = web.handle_request(
        "POST", "/api/login",
        {"Host": "localhost", "Content-Type": "application/json"},
        json.dumps({"passphrase": "correct"}).encode(),
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=True,
        auth_check=lambda c: False,
        login_provider=login_prov,
    )
    assert resp.status == 200
    assert "Set-Cookie" in resp.headers


def test_login_endpoint_failure_returns_401(provider, hosts):
    import json
    def login_prov(passphrase):
        return False, "Incorrect passphrase", {}

    resp = web.handle_request(
        "POST", "/api/login",
        {"Host": "localhost", "Content-Type": "application/json"},
        json.dumps({"passphrase": "wrong"}).encode(),
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=True,
        auth_check=lambda c: False,
        login_provider=login_prov,
    )
    assert resp.status == 401


def test_logout_clears_cookie(provider, hosts):
    resp = web.handle_request(
        "POST", "/api/logout",
        {"Host": "localhost", "Content-Type": "application/json",
         "Cookie": "crr_session=valid"},
        b"{}",
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        auth_enabled=True,
        auth_check=lambda c: c == "valid",
        logout_provider=lambda: {"Set-Cookie": "crr_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"},
    )
    assert resp.status == 200
    assert "Max-Age=0" in resp.headers.get("Set-Cookie", "")
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -v -k "login_endpoint or logout"`
Expected: FAIL

- [ ] **Step 7: Implement login/logout/dashboard-auth endpoints**

In `web.py`'s `handle_request`, in the `POST` section, add before the final `return _plain(404, ...)`:

```python
        if path == "/api/login":
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            passphrase = data.get("passphrase") if isinstance(data, dict) else None
            if not isinstance(passphrase, str) or not passphrase:
                return _plain(400, 'expected {"passphrase": "<string>"}')
            if login_provider is None:
                return _plain(503, "login unavailable")
            ok, error, extra_headers = login_provider(passphrase)
            if ok:
                resp = _json(200, {"ok": True})
                return Response(resp.status, {**resp.headers, **extra_headers}, resp.body)
            return _json(401, {"error": error or "unauthorized"})

        if path == "/api/logout":
            if logout_provider is None:
                return _plain(503, "logout unavailable")
            extra_headers = logout_provider()
            resp = _json(200, {"ok": True})
            return Response(resp.status, {**resp.headers, **extra_headers}, resp.body)

        if path == "/api/dashboard-auth":
            ctype = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _plain(415, "content-type must be application/json")
            try:
                data = json.loads(body or b"")
            except (ValueError, TypeError):
                return _plain(400, "invalid JSON")
            if not isinstance(data, dict) or "op" not in data:
                return _plain(400, 'expected {"op": "..."}')
            if dashboard_auth_provider is None:
                return _plain(503, "dashboard auth unavailable")
            try:
                ok, message = dashboard_auth_provider(data)
            except ValueError as exc:
                return _plain(400, str(exc))
            return _json(200 if ok else 400, {"ok": ok, "message": message})
```

- [ ] **Step 8: Write test for bootstrap_state injection into sessions payload**

Append to `tests/test_web.py`:

```python
def test_bootstrap_state_injected_into_sessions(provider, hosts):
    import json
    resp = web.handle_request(
        "GET", "/api/sessions", {"Host": "localhost"},
        sessions_provider=provider, allowed_hosts=hosts, allowed_suffixes=(),
        bootstrap_state={"login_enabled": False, "bootstrap_dismissed": False},
    )
    data = json.loads(resp.body)
    assert data["login_enabled"] is False
    assert data["bootstrap_dismissed"] is False
```

- [ ] **Step 9: Implement bootstrap_state injection**

In `web.py`'s `handle_request`, in the `GET /api/sessions` branch, after `return _json(200, sessions_provider())`, modify to inject bootstrap state:

```python
        if path == "/api/sessions":
            payload = sessions_provider()
            if bootstrap_state is not None:
                payload.update(bootstrap_state)
            return _json(200, payload)
```

- [ ] **Step 10: Run all web tests**

Run: `.venv/bin/pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 11: Bump PAGE_VERSION**

In `crr/core/web.py`, update:

```python
PAGE_VERSION = 61  # v61: Dashboard login — bootstrap prompt + settings section
```

- [ ] **Step 12: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 13: Commit**

```bash
git add crr/core/web.py tests/test_web.py
git commit -m "feat(web): add auth middleware, login/logout/dashboard-auth endpoints"
```

---

### Task 5: CLI wiring + page.html UI (bootstrap modal + settings section)

**Files:**
- Modify: `crr/cli.py` (wire DashboardAuthStore and providers into `make_web_handler` and `_cmd_web`)
- Modify: `crr/core/page.html` (bootstrap prompt modal + settings login section)
- Modify: `tests/test_cli.py` (at minimum: monkeypatch the new providers in existing web-related tests)
- Modify: `tests/test_page_version_guard.py` (version will have changed)

**Interfaces:**
- Consumes: everything from Tasks 1-4

- [ ] **Step 1: Wire DashboardAuthStore into _cmd_web**

In `crr/cli.py`'s `_cmd_web` function, after the existing stores are created (near line 4243), add:

```python
    from crr.core import dashboard_auth
    auth_store = dashboard_auth.DashboardAuthStore(sd)
    rate_limiter = dashboard_auth.LoginRateLimiter()
    session_hours = config.get("dashboard_session_hours")
    max_age_seconds = session_hours * 3600
```

Create the auth providers:

```python
    def auth_check(cookie_value: str) -> bool:
        secret = auth_store.signing_secret()
        if secret is None:
            return False
        return dashboard_auth.validate_token(cookie_value, secret, max_age_seconds)

    def login_provider(passphrase: str) -> tuple[bool, str, dict[str, str]]:
        # Delay-then-verify: on a rate-limited attempt, sleep for `delay`
        # seconds and then STILL check the passphrase. An early return here
        # (returning "too many attempts" without verifying) would reject the
        # correct passphrase forever once failures reach 5, since `_failures`
        # only decreases via reset() on a successful verify — a real bug
        # found in review, not a hypothetical.
        delay = rate_limiter.check()
        if delay > 0:
            time.sleep(delay)
        if not auth_store.verify(passphrase):
            rate_limiter.record_failure()
            return False, "Incorrect passphrase", {}
        rate_limiter.reset()
        secret = auth_store.signing_secret()
        if secret is None:
            return False, "Login not configured", {}
        token = dashboard_auth.create_token(secret)
        cookie = (f"{dashboard_auth.COOKIE_NAME}={token}; "
                  f"HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age_seconds}")
        return True, "", {"Set-Cookie": cookie}

    def logout_provider() -> dict[str, str]:
        cookie = (f"{dashboard_auth.COOKIE_NAME}=; "
                  f"HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        return {"Set-Cookie": cookie}

    def dashboard_auth_provider(data: dict) -> tuple[bool, str]:
        op = data.get("op")
        if op == "enable":
            auth_store.enable(data.get("passphrase", ""), data.get("confirm", ""))
            return True, "Login enabled"
        if op == "change":
            auth_store.change(data.get("current", ""), data.get("new", ""), data.get("confirm", ""))
            return True, "Passphrase changed"
        if op == "disable":
            auth_store.disable(data.get("current", ""))
            return True, "Login disabled"
        if op == "dismiss-bootstrap":
            auth_store.dismiss_bootstrap()
            return True, "Bootstrap dismissed"
        raise ValueError(f"unknown op: {op}")

    def bootstrap_state_provider() -> dict:
        return {
            "login_enabled": auth_store.login_enabled(),
            "bootstrap_dismissed": auth_store.bootstrap_dismissed(),
        }
```

Thread these into `make_web_handler` — add the new parameters to the `make_web_handler` call and thread them through the `_Handler._dispatch` → `web.handle_request` chain. `make_web_handler` gains:

```python
    auth_enabled: bool = False,
    auth_check: Callable[[str], bool] | None = None,
    login_provider: ...,
    logout_provider: ...,
    dashboard_auth_provider: ...,
    bootstrap_state_fn: Callable[[], dict] | None = None,
```

And in the `_Handler._dispatch` method, pass them through:

```python
    auth_enabled=auth_enabled,
    auth_check=auth_check,
    login_provider=login_provider,
    logout_provider=logout_provider,
    dashboard_auth_provider=dashboard_auth_provider,
    bootstrap_state=bootstrap_state_fn() if bootstrap_state_fn else None,
```

Wire them in `_cmd_web`:

```python
    handler = make_web_handler(
        provider, allowed, (".ts.net",),
        # ... existing providers ...
        auth_enabled=auth_store.login_enabled(),
        auth_check=auth_check,
        login_provider=login_provider,
        logout_provider=logout_provider,
        dashboard_auth_provider=dashboard_auth_provider,
        bootstrap_state_fn=bootstrap_state_provider,
    )
```

Note: `auth_enabled` is read at handler construction time, not per-request. This means enabling/disabling login takes effect after a service restart. This is acceptable — the admin enables login from the Settings modal, sees "restart the service to activate", and `crr deploy` handles it. Alternatively, `auth_enabled` can be checked live per-request by passing `auth_store.login_enabled` as a callable — choose whichever pattern the reviewer prefers, but document the choice.

- [ ] **Step 2: Add bootstrap prompt modal to page.html**

Add a new modal in `crr/core/page.html`, after the `reauthModal` div. This modal is shown when the `/api/sessions` payload contains `login_enabled: false` AND `bootstrap_dismissed: false`:

```html
<div id="bootstrapModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:90; align-items:center; justify-content:center;">
  <div style="background:#12161d; border:1px solid #2f3745; border-radius:8px; padding:24px; max-width:400px; width:90%; box-shadow:0 20px 60px rgba(0,0,0,0.6); color:#e6e6e6;">
    <h3 style="margin:0 0 12px;">Secure this dashboard?</h3>
    <p style="font-size:13px; color:#8a93a2; margin:0 0 16px;">This dashboard can restart and control your Claude sessions. Set a passphrase to require login.</p>
    <div id="bootstrapSetup" style="display:none; margin:0 0 12px;">
      <label style="display:block; font-size:13px; margin:0 0 4px; color:#8a93a2;">Passphrase (8+ characters)</label>
      <input type="password" id="bootstrapPass" style="width:100%; padding:6px; margin:0 0 8px; box-sizing:border-box; background:#1c222c; color:#cdd3dd; border:1px solid #2f3745; border-radius:6px;">
      <label style="display:block; font-size:13px; margin:0 0 4px; color:#8a93a2;">Confirm</label>
      <input type="password" id="bootstrapConfirm" style="width:100%; padding:6px; margin:0 0 8px; box-sizing:border-box; background:#1c222c; color:#cdd3dd; border:1px solid #2f3745; border-radius:6px;">
      <p id="bootstrapError" style="color:#fca5a5; font-size:13px; margin:0 0 8px; display:none;"></p>
      <button onclick="submitBootstrapPassphrase()" style="width:100%; padding:8px; background:#2563eb; color:#fff; border:none; border-radius:6px; cursor:pointer;">Enable login</button>
    </div>
    <div id="bootstrapButtons">
      <button onclick="showBootstrapSetup()" style="width:100%; padding:8px; background:#2563eb; color:#fff; border:none; border-radius:6px; cursor:pointer; margin:0 0 8px;">Set passphrase</button>
      <button onclick="dismissBootstrap()" style="width:100%; padding:8px; background:transparent; color:#8a93a2; border:1px solid #2f3745; border-radius:6px; cursor:pointer;">No login</button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Add bootstrap JS**

In the `<script>` block of `page.html`, add the bootstrap prompt logic:

```javascript
function checkBootstrap(data) {
  var modal = document.getElementById('bootstrapModal');
  if (!modal) return;
  if (data.login_enabled === false && data.bootstrap_dismissed === false) {
    modal.style.display = 'flex';
  } else {
    modal.style.display = 'none';
  }
}

function showBootstrapSetup() {
  document.getElementById('bootstrapButtons').style.display = 'none';
  document.getElementById('bootstrapSetup').style.display = 'block';
  document.getElementById('bootstrapPass').focus();
}

function submitBootstrapPassphrase() {
  var pp = document.getElementById('bootstrapPass').value;
  var confirm = document.getElementById('bootstrapConfirm').value;
  var errEl = document.getElementById('bootstrapError');
  fetch('/api/dashboard-auth', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op: 'enable', passphrase: pp, confirm: confirm})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) {
      document.getElementById('bootstrapModal').style.display = 'none';
    } else {
      errEl.textContent = d.message || 'Failed';
      errEl.style.display = 'block';
    }
  });
}

function dismissBootstrap() {
  fetch('/api/dashboard-auth', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op: 'dismiss-bootstrap'})
  }).then(function() {
    document.getElementById('bootstrapModal').style.display = 'none';
  });
}
```

Call `checkBootstrap(data)` from the existing poll callback (the function that processes `/api/sessions` responses).

- [ ] **Step 4: Add Settings modal login section**

In `page.html`'s `#admin-modal`, add a new section ABOVE the existing `#autokick-section`:

```html
    <div id="login-section" style="padding: 10px 16px 0;">
      <h3 style="margin:0 0 6px; font-size:12px; color:#8a93a2; font-weight:600;">Dashboard Login</h3>
      <div id="login-status"></div>
      <div id="login-setup" style="display:none; margin:8px 0 0;">
        <label style="display:block; font-size:13px; margin:0 0 4px; color:#8a93a2;">Passphrase (8+ characters)</label>
        <input type="password" id="loginSetupPass" style="width:100%; padding:6px; margin:0 0 6px; box-sizing:border-box; background:#1c222c; color:#cdd3dd; border:1px solid #2f3745; border-radius:6px;">
        <label style="display:block; font-size:13px; margin:0 0 4px; color:#8a93a2;">Confirm</label>
        <input type="password" id="loginSetupConfirm" style="width:100%; padding:6px; margin:0 0 6px; box-sizing:border-box; background:#1c222c; color:#cdd3dd; border:1px solid #2f3745; border-radius:6px;">
        <p id="loginSetupError" style="color:#fca5a5; font-size:13px; margin:0 0 6px; display:none;"></p>
        <button onclick="enableLoginFromSettings()" style="padding:6px 12px; background:#2563eb; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:13px;">Enable login</button>
        <button onclick="cancelLoginSetup()" style="padding:6px 12px; background:transparent; color:#8a93a2; border:1px solid #2f3745; border-radius:6px; cursor:pointer; font-size:13px; margin-left:6px;">Cancel</button>
      </div>
      <div id="login-change" style="display:none; margin:8px 0 0;">
        <label style="display:block; font-size:13px; margin:0 0 4px; color:#8a93a2;">Current passphrase</label>
        <input type="password" id="loginChangeCurrent" style="width:100%; padding:6px; margin:0 0 6px; box-sizing:border-box; background:#1c222c; color:#cdd3dd; border:1px solid #2f3745; border-radius:6px;">
        <label style="display:block; font-size:13px; margin:0 0 4px; color:#8a93a2;">New passphrase</label>
        <input type="password" id="loginChangeNew" style="width:100%; padding:6px; margin:0 0 6px; box-sizing:border-box; background:#1c222c; color:#cdd3dd; border:1px solid #2f3745; border-radius:6px;">
        <label style="display:block; font-size:13px; margin:0 0 4px; color:#8a93a2;">Confirm new</label>
        <input type="password" id="loginChangeConfirm" style="width:100%; padding:6px; margin:0 0 6px; box-sizing:border-box; background:#1c222c; color:#cdd3dd; border:1px solid #2f3745; border-radius:6px;">
        <p id="loginChangeError" style="color:#fca5a5; font-size:13px; margin:0 0 6px; display:none;"></p>
        <button onclick="submitChangePassphrase()" style="padding:6px 12px; background:#2563eb; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:13px;">Change</button>
        <button onclick="cancelLoginChange()" style="padding:6px 12px; background:transparent; color:#8a93a2; border:1px solid #2f3745; border-radius:6px; cursor:pointer; font-size:13px; margin-left:6px;">Cancel</button>
      </div>
    </div>
```

- [ ] **Step 5: Add Settings modal login JS**

Add the JavaScript for the Settings modal login controls:

```javascript
function renderLoginSection(data) {
  var status = document.getElementById('login-status');
  if (!status) return;
  if (data.login_enabled) {
    status.innerHTML = '<div style="font-size:13px; color:#cdd3dd; margin:0 0 8px;">Login is enabled</div>'
      + '<button onclick="showLoginChange()" style="padding:4px 10px; background:transparent; color:#9fb0f5; border:1px solid #2f3745; border-radius:6px; cursor:pointer; font-size:12px; margin-right:6px;">Change passphrase</button>'
      + '<button onclick="disableLogin()" style="padding:4px 10px; background:transparent; color:#e0a53b; border:1px solid #2f3745; border-radius:6px; cursor:pointer; font-size:12px; margin-right:6px;">Disable login</button>'
      + '<button onclick="logoutDashboard()" style="padding:4px 10px; background:transparent; color:#8a93a2; border:1px solid #2f3745; border-radius:6px; cursor:pointer; font-size:12px;">Log out</button>';
  } else {
    status.innerHTML = '<div style="font-size:13px; color:#8a93a2; margin:0 0 8px;">Login is not enabled</div>'
      + '<button onclick="showLoginSetup()" style="padding:4px 10px; background:#2563eb; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:12px;">Set passphrase</button>';
  }
  document.getElementById('login-setup').style.display = 'none';
  document.getElementById('login-change').style.display = 'none';
}

function showLoginSetup() {
  document.getElementById('login-setup').style.display = 'block';
  document.getElementById('loginSetupPass').focus();
}
function cancelLoginSetup() {
  document.getElementById('login-setup').style.display = 'none';
}
function enableLoginFromSettings() {
  var pp = document.getElementById('loginSetupPass').value;
  var confirm = document.getElementById('loginSetupConfirm').value;
  var errEl = document.getElementById('loginSetupError');
  fetch('/api/dashboard-auth', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op: 'enable', passphrase: pp, confirm: confirm})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) { cancelLoginSetup(); poll(); }
    else { errEl.textContent = d.message || 'Failed'; errEl.style.display = 'block'; }
  });
}

function showLoginChange() {
  document.getElementById('login-change').style.display = 'block';
  document.getElementById('loginChangeCurrent').focus();
}
function cancelLoginChange() {
  document.getElementById('login-change').style.display = 'none';
}
function submitChangePassphrase() {
  var cur = document.getElementById('loginChangeCurrent').value;
  var np = document.getElementById('loginChangeNew').value;
  var confirm = document.getElementById('loginChangeConfirm').value;
  var errEl = document.getElementById('loginChangeError');
  fetch('/api/dashboard-auth', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op: 'change', current: cur, 'new': np, confirm: confirm})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) { cancelLoginChange(); poll(); }
    else { errEl.textContent = d.message || 'Failed'; errEl.style.display = 'block'; }
  });
}

function disableLogin() {
  var pp = prompt('Enter current passphrase to disable login:');
  if (!pp) return;
  fetch('/api/dashboard-auth', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op: 'disable', current: pp})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) poll();
    else alert(d.message || 'Failed');
  });
}

function logoutDashboard() {
  fetch('/api/logout', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}'
  }).then(function() { location.reload(); });
}
```

Call `renderLoginSection(data)` and `checkBootstrap(data)` from the poll callback that processes `/api/sessions` responses.

- [ ] **Step 6: Update test_page_version_guard.py**

The version guard test pins a content hash of `page.html` against `PAGE_VERSION`. After changing page.html, the test should fail with a message telling you to bump the version — which was done in Task 4 Step 11. Run the test to verify it passes with the new version.

- [ ] **Step 7: Update existing test monkeypatches**

Any existing test in `test_cli.py` that monkeypatches `make_web_handler` or constructs it directly will need the new keyword args. Scan for `make_web_handler` references and add `auth_enabled=False` (or appropriate defaults) to their calls so they don't break.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add crr/cli.py crr/core/page.html crr/core/web.py tests/test_cli.py tests/test_page_version_guard.py
git commit -m "feat(auth): wire dashboard login into CLI, add bootstrap prompt and settings UI"
```

---

## Self-Review

**Spec coverage:**
- Passphrase hashing with scrypt → Task 1 ✓
- Stateless HMAC tokens → Task 1 ✓
- Token validation with timing-safe comparison → Task 1 ✓
- Rate limiting → Task 2 ✓
- DashboardAuthStore (enable/change/disable/dismiss) → Task 2 ✓
- Login page HTML → Task 3 ✓
- `dashboard_session_hours` config key → Task 3 ✓
- Auth middleware in web handler → Task 4 ✓
- POST /api/login, /api/logout, /api/dashboard-auth → Task 4 ✓
- Bootstrap state injection → Task 4 ✓
- Cookie attributes (HttpOnly, SameSite=Strict, no Secure) → Task 5 ✓
- Bootstrap prompt modal → Task 5 ✓
- Settings modal login section → Task 5 ✓
- Exempt endpoints (/api/version, /manifest.webmanifest) → Task 4 ✓
- Corrupt file → fail-open → Task 2 ✓
- Passphrase change invalidates sessions → Task 2 ✓
- PAGE_VERSION bump → Task 4 ✓
- CONFIG_DEFAULTS_VERSION bump + ledger → Task 3 ✓
- DASHBOARD_AUTH_STORE_VERSION → Task 1 ✓

**Placeholder scan:** No TBD, TODO, or vague steps found.

**Type consistency:** `hash_passphrase`, `verify_passphrase`, `create_token`, `validate_token`, `DashboardAuthStore`, `LoginRateLimiter`, `login_page`, `PassphraseError`, `COOKIE_NAME`, `FILENAME` — all used consistently across tasks.
