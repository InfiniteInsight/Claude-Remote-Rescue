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

    monkeypatch.setattr(cloudflared.shutil, "which", lambda _: "/usr/bin/systemctl")
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


def test_start_and_stop_refuse_without_systemd(monkeypatch, tmp_path):
    # No systemd --user on this host (e.g. macOS/Windows): start() must
    # refuse honestly BEFORE writing a unit file or touching cloudflared
    # at all, and stop() must refuse the same way rather than raising a
    # raw errno from a missing systemctl binary.
    def fake_which(name):
        if name == "systemctl":
            return None
        return "/usr/bin/cloudflared"

    monkeypatch.setattr(cloudflared.shutil, "which", fake_which)
    cf = cloudflared.RealCloudflared(2.0, "crr", "crr.example.com", home=tmp_path)

    ok, msg = cf.start(8377)
    assert not ok
    assert "not supported on this host yet" in msg
    unit = tmp_path / ".config" / "systemd" / "user" / cloudflared.UNIT_NAME
    assert not unit.exists()

    ok, msg = cf.stop()
    assert not ok
    assert "not supported on this host yet" in msg


def test_advertise_url_is_static_from_hostname():
    cf = cloudflared.RealCloudflared(2.0, "crr", "crr.example.com")
    assert cf.advertise_url() == "https://crr.example.com/"
    assert cloudflared.RealCloudflared(2.0, "crr", "").advertise_url() is None
