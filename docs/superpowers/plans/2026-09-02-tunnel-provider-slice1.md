# Pluggable Tunnel Support — Slice 1 (Core + CLI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `TunnelProvider` abstraction with Tailscale and Cloudflare (named tunnel) adapters, provider selection via config + settings override, and a `crr tunnel up|down|status` CLI wired into qr / host allowlist / reachable-at-boot.

**Architecture:** New `TunnelProvider` port in `crr/core/ports.py`; pure selection logic in `crr/core/tunnel.py`; adapters `crr/adapters/cloudflared.py` (systemd --user unit `crr-tunnel.service`) and extended `crr/adapters/tailscale.py`; all wiring in `crr/cli.py`. Spec: `docs/superpowers/specs/2026-09-02-tunnel-provider-design.md`.

**Tech Stack:** Python 3.12 stdlib only. pytest via `.venv/bin/pytest`. Branch: `feat/tunnel-provider` (already exists, spec committed).

## Global Constraints

- One-way layering, machine-enforced: `crr.cli` → `crr.adapters` → `crr.core`. Core never imports adapters/cli. Verify with `.venv/bin/lint-imports`.
- Zero runtime dependencies. No new packages.
- TDD: every task writes its failing test first and runs it before implementing.
- Adapter subprocess calls are tri-state: missing binary / timeout / OSError / nonzero exit / unparseable output → `None`/`"unknown"`, never a raise (mirror `RealTailscale._run_json`).
- F16: an unknown probe result must never be treated as a confirmed state; nothing destructive happens on unknown.
- `zombie_strikes`-style versioned defaults: config change bumps `CONFIG_DEFAULTS_VERSION` 24 → 25 with a ledger comment; the pinned test in `tests/test_config.py` is renamed/updated in the same task.
- Commit after each task with `--no-verify` EXCEPT the final task, which commits normally so the pre-commit hook runs the full suite once.
- Commit trailer on every commit:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01JKhoNtnaXXCEictTyXHk2t`

---

### Task 1: Config keys (defaults v25)

**Files:**
- Modify: `crr/core/config.py` (DEFAULTS dict ~line 108; `CONFIG_DEFAULTS_VERSION` ~line 105)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.get("tunnel_provider") -> str` (default `"tailscale"`), `config.get("cloudflare_tunnel_name") -> str` (default `""`), `config.get("cloudflare_hostname") -> str` (default `""`), `cfg.CONFIG_DEFAULTS_VERSION == 25`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`:

```python
def test_tunnel_provider_defaults():
    # Tunnel spec 2026-09-02: default preserves current behavior exactly
    # (tailscale, nothing else configured).
    assert cfg.DEFAULTS["tunnel_provider"] == "tailscale"
    assert cfg.DEFAULTS["cloudflare_tunnel_name"] == ""
    assert cfg.DEFAULTS["cloudflare_hostname"] == ""
```

Also update the pinned version test: in `test_vestigial_keys_are_gone_and_version_bumped`, change the final assert to `assert cfg.CONFIG_DEFAULTS_VERSION == 25` and add a ledger comment line above it:
`# v25 (2026-09-02): tunnel_provider + cloudflare_tunnel_name + cloudflare_hostname (pluggable tunnel spec).`

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: FAIL — `KeyError: 'tunnel_provider'` and `assert 24 == 25`.

- [ ] **Step 3: Implement** — in `crr/core/config.py`:

Add to the version ledger comment block and bump:

```python
# v25: tunnel_provider + cloudflare_tunnel_name + cloudflare_hostname
# (pluggable tunnel support, spec 2026-09-02 — provider default
# "tailscale" preserves pre-tunnel behavior byte-for-byte)
CONFIG_DEFAULTS_VERSION = 25
```

Add to `DEFAULTS` (near the existing `host_allowlist_extras` networking keys):

```python
    # tunnels (spec 2026-09-02): which provider fronts the dashboard.
    # "tailscale" | "cloudflare" | "none" — validated where consumed
    # (crr.core.tunnel), since Config only type-checks against defaults.
    "tunnel_provider": "tailscale",
    "cloudflare_tunnel_name": "",    # `cloudflared tunnel create <name>`
    "cloudflare_hostname": "",       # e.g. "crr.example.com" (DNS-routed)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_config.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crr/core/config.py tests/test_config.py
git commit --no-verify -m "feat(tunnel): config keys — tunnel_provider + cloudflare name/hostname (defaults v25)"
```

---

### Task 2: Pure selection logic — `crr/core/tunnel.py`

**Files:**
- Create: `crr/core/tunnel.py`
- Test: `tests/test_tunnel.py` (new)

**Interfaces:**
- Produces:
  - `TUNNEL_PROVIDERS = ("tailscale", "cloudflare", "none")`
  - `class TunnelSelection(NamedTuple): provider: str; tunnel_name: str; hostname: str; origin: str  # "configured" | "override"`
  - `select(config_provider, config_tunnel_name, config_hostname, override) -> TunnelSelection` — `override` is a `Mapping[str, str|None] | None` (the SettingsStore shape from Task 3); raises `ValueError` naming the bad value when the effective provider is not in `TUNNEL_PROVIDERS`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_tunnel.py`:

```python
"""Pure tunnel-provider selection (spec 2026-09-02).

No I/O: config values and the settings override mapping are passed in;
the cli resolves both and calls select().
"""

import pytest

from crr.core import tunnel


def test_config_only_selection():
    sel = tunnel.select("tailscale", "", "", None)
    assert sel == tunnel.TunnelSelection("tailscale", "", "", "configured")


def test_override_wins_field_by_field():
    sel = tunnel.select(
        "tailscale", "cfg-name", "cfg.example.com",
        {"provider": "cloudflare", "cloudflare_tunnel_name": None,
         "cloudflare_hostname": "crr.example.com"},
    )
    assert sel.provider == "cloudflare"
    assert sel.tunnel_name == "cfg-name"        # None = no override
    assert sel.hostname == "crr.example.com"
    assert sel.origin == "override"


def test_origin_is_configured_when_override_has_no_provider():
    sel = tunnel.select("none", "", "", {"provider": None,
                                          "cloudflare_tunnel_name": None,
                                          "cloudflare_hostname": None})
    assert sel.provider == "none"
    assert sel.origin == "configured"


@pytest.mark.parametrize("bad", ["wireguard", "", "Tailscale"])
def test_unknown_provider_raises_naming_the_value(bad):
    with pytest.raises(ValueError, match=bad or "''"):
        tunnel.select(bad, "", "", None)


def test_unknown_override_provider_raises_too():
    with pytest.raises(ValueError):
        tunnel.select("tailscale", "", "", {"provider": "ngrok",
                                             "cloudflare_tunnel_name": None,
                                             "cloudflare_hostname": None})
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/pytest tests/test_tunnel.py -q`
Expected: FAIL — `ModuleNotFoundError: crr.core.tunnel` (import error at collection counts as the red here; the module doesn't exist).

- [ ] **Step 3: Implement** — create `crr/core/tunnel.py`:

```python
"""Pure tunnel-provider selection (spec 2026-09-02).

Effective value = settings override ?? config default, field by field —
the autokick layering pattern. No I/O here: the cli resolves config and
the SettingsStore and passes plain values in. An unknown provider value
raises ValueError naming it (loud, not laundered): Config only
type-checks strings, so this is where the enum bites.
"""

from __future__ import annotations

from typing import Mapping, NamedTuple

TUNNEL_PROVIDERS = ("tailscale", "cloudflare", "none")


class TunnelSelection(NamedTuple):
    provider: str
    tunnel_name: str
    hostname: str
    origin: str  # "configured" | "override" — where provider came from


def select(
    config_provider: str,
    config_tunnel_name: str,
    config_hostname: str,
    override: Mapping[str, str | None] | None,
) -> TunnelSelection:
    override = override or {}
    provider = override.get("provider") or config_provider
    origin = "override" if override.get("provider") else "configured"
    if provider not in TUNNEL_PROVIDERS:
        raise ValueError(
            f"unknown tunnel provider {provider!r} — expected one of {TUNNEL_PROVIDERS}"
        )
    return TunnelSelection(
        provider=provider,
        tunnel_name=override.get("cloudflare_tunnel_name") or config_tunnel_name,
        hostname=override.get("cloudflare_hostname") or config_hostname,
        origin=origin,
    )
```

- [ ] **Step 4: Run, verify pass** — `.venv/bin/pytest tests/test_tunnel.py -q`

- [ ] **Step 5: Commit**

```bash
git add crr/core/tunnel.py tests/test_tunnel.py
git commit --no-verify -m "feat(tunnel): pure provider selection — override ?? config, loud on unknown"
```

---

### Task 3: SettingsStore tunnel overrides (GUI-writable)

**Files:**
- Modify: `crr/core/settings.py` (class `SettingsStore`, after the autokick methods ~line 230)
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `SettingsStore.read_tunnel() -> dict` with exactly keys `provider`, `cloudflare_tunnel_name`, `cloudflare_hostname` (each `str | None`); `SettingsStore.write_tunnel(provider=None, cloudflare_tunnel_name=None, cloudflare_hostname=None) -> None` (None clears that field's override). Stored under a top-level `"tunnel"` key in settings.json; `SETTINGS_STORE_VERSION` stays 1 (additive optional key, same as `autokick` being optional). A degraded (corrupt) store reads as all-None — same fail-safe as `effective_global_autokick`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_settings.py` (match its existing fixture style; it constructs `SettingsStore(tmp_path)`):

```python
# --- tunnel overrides (spec 2026-09-02): GUI-writable, config is default ---

def test_tunnel_reads_all_none_when_unset(tmp_path):
    store = settings.SettingsStore(tmp_path)
    assert store.read_tunnel() == {
        "provider": None,
        "cloudflare_tunnel_name": None,
        "cloudflare_hostname": None,
    }


def test_tunnel_write_then_read_roundtrip(tmp_path):
    store = settings.SettingsStore(tmp_path)
    store.write_tunnel(provider="cloudflare",
                       cloudflare_tunnel_name="crr",
                       cloudflare_hostname="crr.example.com")
    assert store.read_tunnel() == {
        "provider": "cloudflare",
        "cloudflare_tunnel_name": "crr",
        "cloudflare_hostname": "crr.example.com",
    }


def test_tunnel_write_none_clears_a_field(tmp_path):
    store = settings.SettingsStore(tmp_path)
    store.write_tunnel(provider="cloudflare",
                       cloudflare_tunnel_name="crr",
                       cloudflare_hostname="crr.example.com")
    store.write_tunnel(provider=None,
                       cloudflare_tunnel_name="crr",
                       cloudflare_hostname="crr.example.com")
    assert store.read_tunnel()["provider"] is None


def test_tunnel_write_preserves_autokick_state(tmp_path):
    # Same survives-a-cycle requirement the autokick writers carry: one
    # writer must never discard the other's state.
    store = settings.SettingsStore(tmp_path)
    store.write_global_autokick(False)
    store.write_tunnel(provider="none",
                       cloudflare_tunnel_name=None,
                       cloudflare_hostname=None)
    assert store.read_global_autokick() is False
    store.write_global_autokick(True)
    assert store.read_tunnel()["provider"] == "none"


def test_tunnel_rejects_unknown_provider_string(tmp_path):
    store = settings.SettingsStore(tmp_path)
    with pytest.raises(settings.SettingsError):
        store.write_tunnel(provider="ngrok",
                           cloudflare_tunnel_name=None,
                           cloudflare_hostname=None)


def test_tunnel_degraded_store_reads_all_none(tmp_path):
    (tmp_path / "settings.json").write_text("{corrupt", encoding="utf-8")
    store = settings.SettingsStore(tmp_path)
    assert store.read_tunnel() == {
        "provider": None,
        "cloudflare_tunnel_name": None,
        "cloudflare_hostname": None,
    }
```

- [ ] **Step 2: Run, verify fail** — `.venv/bin/pytest tests/test_settings.py -q` — Expected: FAIL, `AttributeError: read_tunnel`.

- [ ] **Step 3: Implement** — in `crr/core/settings.py`, import `TUNNEL_PROVIDERS` from `crr.core.tunnel` at the top (`from crr.core.tunnel import TUNNEL_PROVIDERS` — core→core, legal), then add to `SettingsStore`:

```python
    # --- tunnel overrides (spec 2026-09-02) -----------------------------
    # GUI-writable overrides over config.toml's tunnel keys. None = "no
    # override; config.toml rules". Stored under one "tunnel" key so the
    # autokick writers and these can never clobber each other's state.

    _TUNNEL_FIELDS = ("provider", "cloudflare_tunnel_name", "cloudflare_hostname")

    def read_tunnel(self) -> dict[str, str | None]:
        raw = self._read_raw().get("tunnel", {})
        if not isinstance(raw, Mapping):
            raw = {}
        return {
            f: (raw.get(f) if isinstance(raw.get(f), str) and raw.get(f) else None)
            for f in self._TUNNEL_FIELDS
        }

    def write_tunnel(
        self,
        provider: str | None = None,
        cloudflare_tunnel_name: str | None = None,
        cloudflare_hostname: str | None = None,
    ) -> None:
        if provider is not None and provider not in TUNNEL_PROVIDERS:
            raise SettingsError(
                f"tunnel provider must be one of {TUNNEL_PROVIDERS}, got {provider!r}"
            )
        for label, value in (("cloudflare_tunnel_name", cloudflare_tunnel_name),
                             ("cloudflare_hostname", cloudflare_hostname)):
            if value is not None and not isinstance(value, str):
                raise SettingsError(f"{label} must be a string or None")
        raw = self._read_raw()
        sessions = _normalize_sessions(raw.get("sessions", {}))
        payload: dict[str, Any] = {
            "v": contracts.SETTINGS_STORE_VERSION, "sessions": sessions,
        }
        if raw.get("autokick") is not None:
            payload["autokick"] = raw["autokick"]
        tunnel_payload = {
            k: v for k, v in (
                ("provider", provider),
                ("cloudflare_tunnel_name", cloudflare_tunnel_name),
                ("cloudflare_hostname", cloudflare_hostname),
            ) if v is not None
        }
        if tunnel_payload:
            payload["tunnel"] = tunnel_payload
        write_json_atomic(self._path, payload)
```

Check `Mapping` is imported in settings.py (`from typing import Any, Mapping` — add if absent). If the existing `_read_raw`-based autokick writers carry the autokick value differently than shown (read the actual `write_session_autokick` tail), mirror exactly what they do so no state is dropped.

- [ ] **Step 4: Run, verify pass** — `.venv/bin/pytest tests/test_settings.py tests/test_tunnel.py -q`

- [ ] **Step 5: Commit**

```bash
git add crr/core/settings.py tests/test_settings.py
git commit --no-verify -m "feat(tunnel): SettingsStore tunnel overrides — GUI-writable, autokick-safe"
```

---

### Task 4: Port + Tailscale provider methods

**Files:**
- Modify: `crr/core/ports.py` (after `TabSpawner`, near `TranscriptSource`)
- Modify: `crr/adapters/tailscale.py` (class `RealTailscale`)
- Test: `tests/test_adapters.py` (append to the existing tailscale section ~line 653)

**Interfaces:**
- Produces (port, `crr/core/ports.py`):

```python
class TunnelHealth(NamedTuple):
    state: str   # "up" | "down" | "unknown"  (F16 tri-state)
    detail: str  # human-readable ("serve not live", "cloudflared not found")


class TunnelProvider(Protocol):
    def name(self) -> str: ...
    def available(self) -> bool: ...
    def start(self, port: int) -> tuple[bool, str]: ...
    def stop(self) -> tuple[bool, str]: ...
    def health(self) -> TunnelHealth: ...
    def advertise_url(self) -> str | None: ...
    def setup_hint(self) -> str | None: ...
```

- Produces (adapter): `RealTailscale` implements all seven methods. `start(port)` runs `tailscale serve --bg <port>`; `stop()` runs `tailscale serve --https=443 off`; `health()` maps serve-status live → `up`, confirmed-not-live → `down`, any probe failure → `unknown`; `advertise_url()` = `tailnet.self_dashboard_url(self.status(), self.serve_status())`; `setup_hint()` returns `"tailscale serve --bg <dashboard_port>"` text (port passed to `__init__` as new optional `dashboard_port: int = 8377` arg — read the actual default from `config.DEFAULTS["dashboard_port"]` at the wiring site, not hardcoded in the adapter call).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_adapters.py` tailscale section:

```python
def test_tailscale_provider_name_and_start_stop_argv(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()

    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", fake_run)
    ts = tailscale.RealTailscale(2.0)
    assert ts.name() == "tailscale"
    ok, _msg = ts.start(8377)
    assert ok
    assert calls[-1] == ["tailscale", "serve", "--bg", "8377"]
    ok, _msg = ts.stop()
    assert ok
    # Non-destructive: only the 443 handler is turned off — NEVER `serve
    # reset`, which clobbers unrelated serve config (spec 2026-09-02).
    assert calls[-1] == ["tailscale", "serve", "--https=443", "off"]


def test_tailscale_provider_health_tri_state(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(ts, "serve_status", lambda: {"TCP": {"443": {}}})
    assert ts.health().state == "up"
    monkeypatch.setattr(ts, "serve_status", lambda: {})
    assert ts.health().state == "down"
    monkeypatch.setattr(ts, "serve_status", lambda: None)
    assert ts.health().state == "unknown"


def test_tailscale_provider_start_fails_without_binary(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: None)
    ts = tailscale.RealTailscale(2.0)
    ok, msg = ts.start(8377)
    assert not ok and "tailscale" in msg
```

- [ ] **Step 2: Run, verify fail** — `.venv/bin/pytest tests/test_adapters.py -q -k tailscale` — Expected: FAIL, `AttributeError: name`.

- [ ] **Step 3: Implement.** Add `TunnelHealth`/`TunnelProvider` to `crr/core/ports.py` exactly as in Interfaces (with a docstring noting the F16 tri-state and that `health()`/probes never raise). In `crr/adapters/tailscale.py` add to `RealTailscale`:

```python
    def name(self) -> str:
        return "tailscale"

    def _run_ok(self, argv: list[str]) -> tuple[bool, str]:
        """Tri-state command runner for the lifecycle verbs: (ok, message).
        Never raises — same degrade contract as _run_json."""
        if shutil.which("tailscale") is None:
            return False, "tailscale binary not found on PATH"
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{argv[0]} failed: {exc}"
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "").strip() or \
                f"{' '.join(argv)} exited {result.returncode}"
        return True, "ok"

    def start(self, port: int) -> tuple[bool, str]:
        return self._run_ok(["tailscale", "serve", "--bg", str(port)])

    def stop(self) -> tuple[bool, str]:
        # Only the 443 handler — `serve reset` would clobber unrelated
        # serve config on this node (spec 2026-09-02).
        return self._run_ok(["tailscale", "serve", "--https=443", "off"])

    def health(self) -> TunnelHealth:
        serve = self.serve_status()
        if serve is None:
            return TunnelHealth("unknown", "tailscale serve status unavailable")
        if serve:
            return TunnelHealth("up", "tailscale serve is live")
        return TunnelHealth("down", "tailscale serve not configured")

    def advertise_url(self) -> str | None:
        from crr.core import tailnet
        return tailnet.self_dashboard_url(self.status(), self.serve_status())

    def setup_hint(self) -> str | None:
        return f"tailscale serve --bg {self._dashboard_port}"
```

`__init__` gains `dashboard_port: int = 8377` stored as `self._dashboard_port` (existing single-arg constructions keep working). Import `TunnelHealth` from `crr.core.ports` at the top of the adapter (adapters→core, legal). Note `serve_status()` live-vs-empty semantics: reuse whatever `tailnet.self_dashboard_url` treats as "serve live" — read that function (crr/core/tailnet.py:17) and keep `health()` consistent with it (falsy dict = not live).

- [ ] **Step 4: Run, verify pass** — `.venv/bin/pytest tests/test_adapters.py tests/test_tailnet.py -q`

- [ ] **Step 5: Commit**

```bash
git add crr/core/ports.py crr/adapters/tailscale.py tests/test_adapters.py
git commit --no-verify -m "feat(tunnel): TunnelProvider port + tailscale lifecycle methods"
```

---

### Task 5: Cloudflared adapter (systemd --user unit)

**Files:**
- Create: `crr/adapters/cloudflared.py`
- Test: `tests/test_cloudflared.py` (new)

**Interfaces:**
- Consumes: `TunnelHealth` from `crr.core.ports`; `systemd.unit_dir(home)` from `crr/adapters/systemd.py` (returns the systemd user unit Path).
- Produces: `RealCloudflared(timeout, tunnel_name, hostname, home=None)` implementing the full `TunnelProvider` port; module constant `UNIT_NAME = "crr-tunnel.service"`; pure function `unit_content(cloudflared_bin, port, tunnel_name) -> str`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_cloudflared.py`:

```python
"""Cloudflared adapter — named tunnel as a systemd --user unit.

All subprocess calls stubbed; tri-state on every failure mode (house
adapter contract). One-time CF account setup (login/create/route dns)
is deliberately NOT automated — start() refuses with the exact hint.
"""

import pytest

from crr.adapters import cloudflared


def _ok(argv, **kw):
    class R: returncode = 0; stdout = "ok"; stderr = ""
    return R()


def test_name_and_unavailable_without_binary(monkeypatch):
    monkeypatch.setattr(cloudflared.shutil, "which", lambda _: None)
    cf = cloudflared.RealCloudflared(2.0, "crr", "crr.example.com")
    assert cf.name() == "cloudflare"
    assert cf.available() is False


def test_start_refuses_without_config_fields(monkeypatch):
    monkeypatch.setattr(cloudflared.shutil, "which", lambda _: "/usr/bin/cloudflared")
    cf = cloudflared.RealCloudflared(2.0, "", "")
    ok, msg = cf.start(8377)
    assert not ok
    assert "cloudflare_tunnel_name" in msg and "cloudflare_hostname" in msg


def test_start_refuses_when_tunnel_info_fails(monkeypatch, tmp_path):
    # `cloudflared tunnel info <name>` nonzero = credentials/tunnel absent:
    # the one-time login/create/route setup has not been done.
    monkeypatch.setattr(cloudflared.shutil, "which", lambda _: "/usr/bin/cloudflared")

    def fake_run(argv, **kw):
        class R: returncode = 1; stdout = ""; stderr = "tunnel not found"
        return R()

    monkeypatch.setattr(cloudflared.subprocess, "run", fake_run)
    cf = cloudflared.RealCloudflared(2.0, "crr", "crr.example.com", home=tmp_path)
    ok, msg = cf.start(8377)
    assert not ok
    assert "cloudflared tunnel login" in msg  # names the setup steps


def test_start_writes_unit_and_enables(monkeypatch, tmp_path):
    monkeypatch.setattr(cloudflared.shutil, "which", lambda _: "/usr/bin/cloudflared")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()

    monkeypatch.setattr(cloudflared.subprocess, "run", fake_run)
    cf = cloudflared.RealCloudflared(2.0, "crr", "crr.example.com", home=tmp_path)
    ok, msg = cf.start(8377)
    assert ok, msg
    unit = tmp_path / ".config" / "systemd" / "user" / cloudflared.UNIT_NAME
    assert unit.is_file()
    text = unit.read_text()
    assert "/usr/bin/cloudflared tunnel --url http://127.0.0.1:8377 run crr" in text
    assert "Restart=on-failure" in text
    flat = ["\0".join(c) for c in calls]
    assert any("daemon-reload" in f for f in flat)
    assert any("enable\0--now\0" + cloudflared.UNIT_NAME in f for f in flat)


def test_stop_disables_unit(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()

    monkeypatch.setattr(cloudflared.subprocess, "run", fake_run)
    cf = cloudflared.RealCloudflared(2.0, "crr", "crr.example.com", home=tmp_path)
    ok, _ = cf.stop()
    assert ok
    assert calls[-1] == ["systemctl", "--user", "disable", "--now",
                         cloudflared.UNIT_NAME]


def test_health_tri_state(monkeypatch, tmp_path):
    cf = cloudflared.RealCloudflared(2.0, "crr", "crr.example.com", home=tmp_path)

    def active(argv, **kw):
        class R: returncode = 0; stdout = "active\n"; stderr = ""
        return R()

    def inactive(argv, **kw):
        class R: returncode = 3; stdout = "inactive\n"; stderr = ""
        return R()

    def boom(argv, **kw):
        raise OSError("no systemctl")

    monkeypatch.setattr(cloudflared.subprocess, "run", active)
    assert cf.health().state == "up"
    monkeypatch.setattr(cloudflared.subprocess, "run", inactive)
    assert cf.health().state == "down"
    monkeypatch.setattr(cloudflared.subprocess, "run", boom)
    assert cf.health().state == "unknown"


def test_advertise_url_is_static_from_hostname():
    cf = cloudflared.RealCloudflared(2.0, "crr", "crr.example.com")
    assert cf.advertise_url() == "https://crr.example.com/"
    assert cloudflared.RealCloudflared(2.0, "crr", "").advertise_url() is None
```

- [ ] **Step 2: Run, verify fail** — `.venv/bin/pytest tests/test_cloudflared.py -q` — Expected: FAIL at import (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** — create `crr/adapters/cloudflared.py`:

```python
"""Cloudflared adapter — a NAMED Cloudflare Tunnel as a systemd --user unit.

Lifecycle host is systemd, not crr-web (spec 2026-09-02, approach A):
the unit survives crr-web restarts and reboots, and supervision stays
where the platform already does it. One-time Cloudflare account setup
(`cloudflared tunnel login` / `tunnel create` / `tunnel route dns`) is
deliberately manual — start() refuses with the exact steps rather than
driving CF auth. All subprocess wrappers are tri-state and never raise
(house adapter contract, mirrors RealTailscale).

Linux-only for now: on hosts without systemd --user this adapter reports
unavailable with an honest hint; launchd/schtasks ports follow the
existing per-OS adapter pattern later.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from crr.core.ports import TunnelHealth

UNIT_NAME = "crr-tunnel.service"

_SETUP_STEPS = (
    "one-time Cloudflare setup:\n"
    "  1. cloudflared tunnel login\n"
    "  2. cloudflared tunnel create <name>\n"
    "  3. cloudflared tunnel route dns <name> <hostname>\n"
    "then re-run: crr tunnel up"
)


def unit_content(cloudflared_bin: str, port: int, tunnel_name: str) -> str:
    """The crr-tunnel.service unit text. Pure so the test pins it."""
    return (
        "[Unit]\n"
        "Description=Claude-Remote-Rescue Cloudflare tunnel (generated by crr)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={cloudflared_bin} tunnel --url http://127.0.0.1:{port} "
        f"run {tunnel_name}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


class RealCloudflared:
    def __init__(self, timeout: float, tunnel_name: str, hostname: str,
                 home: Path | None = None) -> None:
        self._timeout = timeout
        self._tunnel_name = tunnel_name
        self._hostname = hostname
        self._home = home or Path.home()

    def name(self) -> str:
        return "cloudflare"

    def available(self) -> bool:
        return shutil.which("cloudflared") is not None

    def _run(self, argv: list[str]) -> tuple[bool, str]:
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{argv[0]} failed: {exc}"
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "").strip() or \
                f"{' '.join(argv)} exited {result.returncode}"
        return True, (result.stdout or "").strip()

    def _unit_path(self) -> Path:
        return self._home / ".config" / "systemd" / "user" / UNIT_NAME

    def start(self, port: int) -> tuple[bool, str]:
        cf_bin = shutil.which("cloudflared")
        if cf_bin is None:
            return False, "cloudflared binary not found on PATH — install it first"
        if not self._tunnel_name or not self._hostname:
            return False, ("cloudflare_tunnel_name and cloudflare_hostname must "
                           "be set (config.toml or dashboard settings)")
        ok, msg = self._run(["cloudflared", "tunnel", "info", self._tunnel_name])
        if not ok:
            return False, (f"tunnel {self._tunnel_name!r} not usable ({msg}) — "
                           + _SETUP_STEPS)
        unit = self._unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(unit_content(cf_bin, port, self._tunnel_name),
                        encoding="utf-8")
        ok, msg = self._run(["systemctl", "--user", "daemon-reload"])
        if not ok:
            return False, f"daemon-reload failed: {msg}"
        ok, msg = self._run(["systemctl", "--user", "enable", "--now", UNIT_NAME])
        if not ok:
            return False, f"enable --now failed: {msg}"
        return True, f"{UNIT_NAME} enabled and started"

    def stop(self) -> tuple[bool, str]:
        return self._run(["systemctl", "--user", "disable", "--now", UNIT_NAME])

    def health(self) -> TunnelHealth:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", UNIT_NAME],
                capture_output=True, text=True, timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return TunnelHealth("unknown", f"systemctl unavailable: {exc}")
        state = (result.stdout or "").strip()
        if state == "active":
            return TunnelHealth("up", f"{UNIT_NAME} active")
        if state in ("inactive", "failed", "deactivating"):
            return TunnelHealth("down", f"{UNIT_NAME} {state}")
        return TunnelHealth("unknown", f"{UNIT_NAME} state {state!r}")

    def advertise_url(self) -> str | None:
        if not self._hostname:
            return None
        return f"https://{self._hostname}/"

    def setup_hint(self) -> str | None:
        return _SETUP_STEPS
```

Note: the unit path derivation duplicates `systemd.unit_dir(home)` — check that helper's actual body first; if it returns `home / ".config" / "systemd" / "user"`, import and use it instead of the inline `_unit_path` (DRY). Adjust the test's expected path accordingly (it already matches that layout).

- [ ] **Step 4: Run, verify pass** — `.venv/bin/pytest tests/test_cloudflared.py -q`

- [ ] **Step 5: Layering check + commit**

Run: `.venv/bin/lint-imports` — Expected: contract kept.

```bash
git add crr/adapters/cloudflared.py tests/test_cloudflared.py
git commit --no-verify -m "feat(tunnel): cloudflared adapter — named tunnel via systemd --user unit"
```

---

### Task 6: `crr tunnel up|down|status` CLI

**Files:**
- Modify: `crr/cli.py` — subparser registration (near the `web`/`qr` parsers ~line 446) + new `_cmd_tunnel` handler + a `_tunnel_provider(config)` resolver helper
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `tunnel.select(...)` (Task 2), `SettingsStore.read_tunnel()` (Task 3), `RealTailscale` port methods (Task 4), `RealCloudflared` (Task 5).
- Produces: `_tunnel_selection(config, sd) -> tunnel.TunnelSelection` and `_tunnel_provider(config, selection)` returning the adapter instance or `None` for `"none"`. Used again by Tasks 7–9.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py`:

```python
# --- crr tunnel (spec 2026-09-02) -----------------------------------------

class _FakeTunnelProvider:
    def __init__(self, name="cloudflare", health_state="up",
                 url="https://crr.example.com/", start_ok=True):
        self._name, self._state, self._url = name, health_state, url
        self._start_ok = start_ok
        self.started_with = None
        self.stopped = False

    def name(self):
        return self._name

    def available(self):
        return True

    def start(self, port):
        self.started_with = port
        return (self._start_ok, "started" if self._start_ok else "missing prereqs")

    def stop(self):
        self.stopped = True
        return True, "stopped"

    def health(self):
        from crr.core.ports import TunnelHealth
        return TunnelHealth(self._state, f"fake {self._state}")

    def advertise_url(self):
        return self._url

    def setup_hint(self):
        return "run the setup"


def test_tunnel_up_starts_active_provider_and_prints_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider()
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    rc = cli.main(["tunnel", "up"])
    out = capsys.readouterr().out
    assert rc == 0
    assert fake.started_with == cli.cfg.DEFAULTS["dashboard_port"]
    assert "https://crr.example.com/" in out


def test_tunnel_up_refusal_prints_message_and_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider(start_ok=False)
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    rc = cli.main(["tunnel", "up"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "missing prereqs" in err


def test_tunnel_status_reports_provider_health_and_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider(health_state="down", url=None)
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    rc = cli.main(["tunnel", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cloudflare" in out and "down" in out


def test_tunnel_down_stops_provider(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider()
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    rc = cli.main(["tunnel", "down"])
    assert rc == 0
    assert fake.stopped


def test_tunnel_none_provider_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / "x").mkdir()  # ensure tmp_path usable as sd
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: None)
    rc = cli.main(["tunnel", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "none" in out


def test_tunnel_settings_override_reaches_selection(tmp_path, monkeypatch):
    # The GUI-written override must actually flow into selection: write
    # provider=cloudflare into the settings store, then check the resolver.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    from crr.core import settings as settings_mod
    settings_mod.SettingsStore(tmp_path).write_tunnel(
        provider="cloudflare", cloudflare_tunnel_name="crr",
        cloudflare_hostname="crr.example.com")
    sel = cli._tunnel_selection(cli.cfg.Config(), tmp_path)
    assert sel.provider == "cloudflare"
    assert sel.hostname == "crr.example.com"
    assert sel.origin == "override"
```

- [ ] **Step 2: Run, verify fail** — `.venv/bin/pytest tests/test_cli.py -q -k tunnel` — Expected: FAIL (`_tunnel_provider` attribute missing / argparse unknown command).

- [ ] **Step 3: Implement.** In `crr/cli.py`:

Subparser (next to the `web` parser registration):

```python
    tun = sub.add_parser("tunnel", help="manage the dashboard tunnel (tailscale/cloudflare)")
    tun.add_argument("action", choices=("up", "down", "status"))
    tun.set_defaults(func=_cmd_tunnel)
```

Resolver helpers + handler (place near `_cmd_qr`; import `tunnel` in the `from crr.core import ...` line and `cloudflared` in the adapters import line):

```python
def _tunnel_selection(config: cfg.Config, sd) -> tunnel.TunnelSelection:
    """Effective tunnel selection: SettingsStore override ?? config default
    (the autokick layering pattern; spec 2026-09-02)."""
    override = settings.SettingsStore(sd).read_tunnel()
    return tunnel.select(
        config.get("tunnel_provider"),
        config.get("cloudflare_tunnel_name"),
        config.get("cloudflare_hostname"),
        override,
    )


def _tunnel_provider(config: cfg.Config, sel: tunnel.TunnelSelection):
    """The adapter for ``sel.provider`` — None for "none"."""
    timeout = config.get("interop_timeout_seconds")
    if sel.provider == "tailscale":
        return tailscale.RealTailscale(timeout,
                                       dashboard_port=config.get("dashboard_port"))
    if sel.provider == "cloudflare":
        return cloudflared.RealCloudflared(timeout, sel.tunnel_name, sel.hostname)
    return None


def _cmd_tunnel(args: argparse.Namespace) -> int:
    config = _load_config()
    sd = state_dir.state_dir()
    try:
        sel = _tunnel_selection(config, sd)
    except ValueError as exc:
        print(f"crr tunnel: {exc}", file=sys.stderr)
        return 2
    provider = _tunnel_provider(config, sel)
    if provider is None:
        print(f"tunnel provider: none ({sel.origin}) — nothing to manage")
        return 0
    if args.action == "up":
        ok, msg = provider.start(config.get("dashboard_port"))
        if not ok:
            print(f"crr tunnel up: {msg}", file=sys.stderr)
            return 2
        url = provider.advertise_url()
        print(msg)
        if url:
            print(url)
        return 0
    if args.action == "down":
        ok, msg = provider.stop()
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    # status — and name any OTHER provider still up, so switching never
    # tears something down behind the user's back (spec 2026-09-02).
    h = provider.health()
    print(f"provider: {provider.name()} ({sel.origin})")
    print(f"health: {h.state} — {h.detail}")
    url = provider.advertise_url()
    print(f"url: {url}" if url else "url: (not derivable)")
    for other_name in ("tailscale", "cloudflare"):
        if other_name == sel.provider:
            continue
        other = _tunnel_provider(
            config, tunnel.TunnelSelection(other_name, sel.tunnel_name,
                                           sel.hostname, sel.origin))
        if other is not None and other.available() and other.health().state == "up":
            print(f"note: {other_name} is also up — `crr tunnel down` does not "
                  f"touch it; switch tunnel_provider and run down to stop it")
    return 0
```

Add one more test for that behavior in Step 1:

```python
def test_tunnel_status_names_the_other_provider_when_it_is_also_up(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    active = _FakeTunnelProvider(name="cloudflare")
    other = _FakeTunnelProvider(name="tailscale", health_state="up")

    def pick(config, sel):
        return active if sel.provider == "cloudflare" else other

    monkeypatch.setattr(cli, "_tunnel_provider", pick)
    from crr.core import settings as settings_mod
    settings_mod.SettingsStore(tmp_path).write_tunnel(
        provider="cloudflare", cloudflare_tunnel_name="crr",
        cloudflare_hostname="crr.example.com")
    rc = cli.main(["tunnel", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tailscale is also up" in out
```

- [ ] **Step 4: Run, verify pass** — `.venv/bin/pytest tests/test_cli.py -q -k tunnel`

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit --no-verify -m "feat(tunnel): crr tunnel up|down|status with settings-aware provider resolution"
```

---

### Task 7: `crr qr` + dashboard QR follow the active provider

**Files:**
- Modify: `crr/cli.py` — `_cmd_qr` (~line 1569) and `qr_svg_provider` (~line 4596)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_tunnel_selection`, `_tunnel_provider` (Task 6).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py`:

```python
def test_qr_uses_active_tunnel_provider_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider(url="https://crr.example.com/")
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    rc = cli.main(["qr"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "https://crr.example.com/" in out


def test_qr_falls_back_to_provider_setup_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider(url=None)
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    rc = cli.main(["qr"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "run the setup" in out          # provider.setup_hint()
    assert "127.0.0.1" in out              # loopback line survives


def test_qr_provider_none_prints_loopback_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: None)
    rc = cli.main(["qr"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "127.0.0.1" in out
```

- [ ] **Step 2: Run, verify fail** — `.venv/bin/pytest tests/test_cli.py -q -k "qr_uses or qr_falls or qr_provider"` — Expected: FAIL (qr still hardcodes tailscale; fake never consulted).

- [ ] **Step 3: Implement.** Replace `_cmd_qr`'s body:

```python
def _cmd_qr(_args: argparse.Namespace) -> int:
    """Print a scannable QR of this machine's dashboard URL via the ACTIVE
    tunnel provider (spec 2026-09-02) — tailscale by default, so the
    pre-tunnel behavior is unchanged. Degrades informationally (rc 0)."""
    config = _load_config()
    sd = state_dir.state_dir()
    try:
        sel = _tunnel_selection(config, sd)
    except ValueError as exc:
        print(f"crr qr: {exc}", file=sys.stderr)
        return 2
    provider = _tunnel_provider(config, sel)
    url = provider.advertise_url() if provider is not None else None
    if url is None:
        port = config.get("dashboard_port")
        print(f"http://127.0.0.1:{port}/  (loopback only)")
        hint = provider.setup_hint() if provider is not None else None
        if hint:
            print(f"To reach it from your phone:  {hint}")
        return 0
    print(qr.to_terminal(url))
    print(url)
    return 0
```

And in the web wiring, change `qr_svg_provider` to source the same way (the `ts_adapter` local stays for `machines_provider`, which remains tailnet-only by design):

```python
    def qr_svg_provider() -> str | None:
        # Lazy (real subprocess probes) — only runs when "Add a device"
        # is opened. Follows the ACTIVE tunnel provider (spec 2026-09-02).
        try:
            sel = _tunnel_selection(config, sd)
        except ValueError:
            return None
        provider = _tunnel_provider(config, sel)
        url = provider.advertise_url() if provider is not None else None
        return qr.to_svg(url) if url else None
```

(Confirm `sd` is in scope at that closure — the web wiring function has `sd = state_dir.state_dir()` earlier; use whatever local name it holds.)

- [ ] **Step 4: Run, verify pass** — `.venv/bin/pytest tests/test_cli.py -q` (full file: the old qr tests, if any assert tailscale specifics, must still pass — the default provider IS tailscale, so behavior is unchanged; fix only tests that stubbed `cli.tailscale.RealTailscale` by keeping that class the default path).

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit --no-verify -m "feat(tunnel): qr + dashboard QR follow the active tunnel provider"
```

---

### Task 8: Host allowlist admits the Cloudflare hostname

**Files:**
- Modify: `crr/cli.py` — the allowlist construction (`allowed = {"127.0.0.1", ...}` ~line 4963)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_tunnel_selection` (Task 6).

- [ ] **Step 1: Write the failing test** — append to `tests/test_cli.py`. First read how existing web-handler tests construct the handler/allowlist (search `make_web_handler` uses in the test file and mirror the cheapest existing pattern). If no direct unit seam exists, test the resolver output instead:

```python
def test_effective_cloudflare_hostname_joins_allowlist(tmp_path, monkeypatch):
    # The dashboard must accept Host: <cloudflare_hostname> without a
    # manual host_allowlist_extras edit (spec 2026-09-02).
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    from crr.core import settings as settings_mod
    settings_mod.SettingsStore(tmp_path).write_tunnel(
        provider="cloudflare", cloudflare_tunnel_name="crr",
        cloudflare_hostname="CRR.Example.Com")
    allowed = cli._web_allowed_hosts(cli.cfg.Config(), tmp_path)
    assert "crr.example.com" in allowed          # lowercased
    assert "127.0.0.1" in allowed                # baseline intact
```

- [ ] **Step 2: Run, verify fail** — Expected: FAIL, `AttributeError: _web_allowed_hosts`.

- [ ] **Step 3: Implement.** Extract the existing inline allowlist construction into a helper and extend it:

```python
def _web_allowed_hosts(config: cfg.Config, sd) -> set[str]:
    """Host allowlist: loopback + hostname + config extras + the effective
    cloudflare hostname (spec 2026-09-02 — no manual extras edit needed).
    A bad tunnel config never breaks web startup: selection errors are
    ignored here (the tunnel commands report them loudly)."""
    allowed = {"127.0.0.1", "localhost", "[::1]", socket.gethostname().lower()}
    allowed.update(h.lower() for h in config.get("host_allowlist_extras"))
    try:
        sel = _tunnel_selection(config, sd)
    except ValueError:
        return allowed
    if sel.hostname:
        allowed.add(sel.hostname.lower())
    return allowed
```

Replace the inline block at the call site with `allowed = _web_allowed_hosts(config, sd)` (keep the `(".ts.net",)` suffix argument to `make_web_handler` unchanged).

- [ ] **Step 4: Run, verify pass** — `.venv/bin/pytest tests/test_cli.py tests/test_web.py -q`

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit --no-verify -m "feat(tunnel): cloudflare hostname auto-joins the dashboard host allowlist"
```

---

### Task 9: `reachable-at-boot` reports tunnel health

**Files:**
- Modify: `crr/cli.py` — `_reachable_at_boot_report` (~line 5419)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_tunnel_selection`, `_tunnel_provider` (Task 6).

- [ ] **Step 1: Write the failing test** — find the existing `reachable-at-boot` report tests in `tests/test_cli.py` (search `reachable-at-boot`) and mirror their monkeypatch setup for boot facts, then add:

```python
def test_reachable_at_boot_reports_tunnel_health_tri_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    fake = _FakeTunnelProvider(health_state="unknown")
    monkeypatch.setattr(cli, "_tunnel_provider", lambda config, sel: fake)
    # reuse the boot-facts monkeypatching from the nearest existing
    # reachable-at-boot test verbatim so the report path runs
    rc = cli.main(["reachable-at-boot"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tunnel: cloudflare — unknown" in out   # unknown stays unknown (F16)
```

- [ ] **Step 2: Run, verify fail** — Expected: FAIL, "tunnel:" line absent.

- [ ] **Step 3: Implement.** At the end of `_reachable_at_boot_report`'s output (before its return), add:

```python
    try:
        sel = _tunnel_selection(config, state_dir.state_dir())
        provider = _tunnel_provider(config, sel)
    except ValueError as exc:
        print(f"tunnel: misconfigured — {exc}")
    else:
        if provider is None:
            print("tunnel: none")
        else:
            h = provider.health()
            print(f"tunnel: {provider.name()} — {h.state} ({h.detail})")
```

- [ ] **Step 4: Run, verify pass** — `.venv/bin/pytest tests/test_cli.py -q -k reachable`

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit --no-verify -m "feat(tunnel): reachable-at-boot names the active tunnel's health"
```

---

### Task 10: Runbook doc, todo.md, full-suite gate

**Files:**
- Create: `docs/tunnels.md`
- Modify: `todo.md` (the "Pluggable tunnel support" item)

- [ ] **Step 1: Write `docs/tunnels.md`:**

```markdown
# Tunnels: reaching the dashboard from outside

The dashboard binds loopback only; a tunnel provider proxies it out.
Pick the provider in `config.toml` (`tunnel_provider = "tailscale" |
"cloudflare" | "none"`) or the dashboard Settings (override wins).

## Tailscale (default)

`crr tunnel up` runs `tailscale serve --bg <port>`. URL:
`https://<node>.<tailnet>.ts.net/`. `crr tunnel down` turns off only the
443 handler — it never runs `serve reset`.

## Cloudflare named tunnel

One-time setup (manual — crr never drives Cloudflare auth):

1. `cloudflared tunnel login`
2. `cloudflared tunnel create <name>`
3. `cloudflared tunnel route dns <name> <hostname>`
4. Set `cloudflare_tunnel_name` + `cloudflare_hostname` (+
   `tunnel_provider = "cloudflare"`) in config.toml or the dashboard.
5. `crr tunnel up` — installs and enables the `crr-tunnel.service`
   systemd --user unit running
   `cloudflared tunnel --url http://127.0.0.1:<port> run <name>`.

**Strongly recommended:** protect `<hostname>` with a Cloudflare Access
policy (Zero Trust → Access; free for up to 50 users). crr does not
verify this. The dashboard's own passphrase login remains the last line
either way.

## Notes

- `crr tunnel status` shows the active provider, tri-state health, and
  the advertised URL. Switching providers does not stop the old one —
  run `crr tunnel down` first if you want it gone.
- The Cloudflare hostname is admitted to the dashboard's Host allowlist
  automatically.
- Cloudflare lifecycle is Linux (systemd --user) for now; macOS/Windows
  report an honest "not supported yet".
```

- [ ] **Step 2: Update `todo.md`** — under the tunnel item add a sub-line:
`      Slice 1 (core + CLI) landed <PR#>; slice 2 (dashboard GUI settings panel) remains.`
(Leave the checkbox unchecked until slice 2 lands.)

- [ ] **Step 3: Full gates**

Run: `.venv/bin/pytest -q` — Expected: all pass (platform skips only).
Run: `.venv/bin/lint-imports` — Expected: contract kept.
Check `tests/test_docs_site.py` didn't break from adding `docs/tunnels.md` (run it; if it pins a docs index, follow its pattern to register the page).

- [ ] **Step 4: Final commit (hook ON — full suite runs)**

```bash
git add docs/tunnels.md todo.md
git commit -m "docs(tunnel): runbook for tailscale/cloudflare tunnel providers"
```

- [ ] **Step 5: PR**

Push `feat/tunnel-provider`, open a PR titled
`feat: pluggable tunnel support — slice 1 (TunnelProvider port, cloudflared unit, crr tunnel CLI)`
whose body summarizes the spec decisions (full lifecycle, named tunnel only, Access out of scope, config+settings selection) and names slice 2 as follow-up. Do NOT merge — leave for review per session policy at the time of execution.
```
