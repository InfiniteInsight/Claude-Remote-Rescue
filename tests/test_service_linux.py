from pathlib import Path

from crr import service_linux


def test_web_service_unit_has_self_sufficient_path(monkeypatch):
    monkeypatch.setattr(
        service_linux.shutil, "which", lambda name: "/usr/local/bin/%s" % name
    )
    text = service_linux.web_service_unit("/opt/crr/bin/crr")
    assert "ExecStart=/opt/crr/bin/crr web" in text
    assert "Environment=PATH=" in text
    # PATH line must include the crr binary's own dir and every external
    # binary the units transitively rely on.
    path_line = [ln for ln in text.splitlines() if ln.startswith("Environment=PATH=")][0]
    assert "/opt/crr/bin" in path_line
    assert "/usr/local/bin" in path_line  # where tmux/ps/claude resolved to
    assert "[Install]" in text
    assert "WantedBy=default.target" in text


def test_watchdog_service_unit_runs_revive_all(monkeypatch):
    monkeypatch.setattr(service_linux.shutil, "which", lambda name: None)
    text = service_linux.watchdog_service_unit("/opt/crr/bin/crr")
    assert "ExecStart=/opt/crr/bin/crr revive --all" in text
    assert "Type=oneshot" in text
    assert "Environment=PATH=" in text
    # Even with no external binaries resolvable, the fallback dirs are
    # still present so the unit isn't left with an empty PATH.
    path_line = [ln for ln in text.splitlines() if ln.startswith("Environment=PATH=")][0]
    for fallback in ("/usr/local/bin", "/usr/bin", "/bin"):
        assert fallback in path_line


def test_watchdog_timer_runs_every_two_minutes():
    text = service_linux.watchdog_timer_unit()
    assert "OnUnitActiveSec=2min" in text
    assert "OnBootSec=2min" in text
    assert "Unit=crr-watchdog.service" in text
    assert "WantedBy=timers.target" in text


def test_resolve_unit_path_dedupes_and_orders(monkeypatch):
    # tmux and ps "resolve" into the same directory as crr_bin; claude
    # resolves elsewhere; unknown binaries resolve to None and are skipped.
    def fake_which(name):
        return {"tmux": "/opt/crr/bin/tmux", "ps": "/usr/bin/ps", "claude": None}.get(name)

    monkeypatch.setattr(service_linux.shutil, "which", fake_which)
    path = service_linux.resolve_unit_path("/opt/crr/bin/crr")
    dirs = path.split(":")
    assert dirs.count("/opt/crr/bin") == 1  # deduped even though crr+tmux share it
    assert "/usr/bin" in dirs
    assert "/usr/local/bin" in dirs and "/bin" in dirs  # fallbacks present


def test_systemd_user_dir_respects_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(service_linux.UNIT_DIR_ENV, str(tmp_path / "units"))
    assert service_linux.systemd_user_dir() == tmp_path / "units"


def test_install_writes_unit_files_without_enabling(tmp_path, monkeypatch):
    """Unit-file generation only -- systemctl/loginctl calls are expected
    to fail loudly in this sandbox (no systemd session) and that failure
    must be reported, not swallowed."""
    monkeypatch.setenv(service_linux.UNIT_DIR_ENV, str(tmp_path / "units"))
    monkeypatch.setattr(service_linux.shutil, "which", lambda name: None)

    steps = service_linux.install("/opt/crr/bin/crr")
    step_names = [s["step"] for s in steps]
    assert "write-units" in step_names
    write_step = next(s for s in steps if s["step"] == "write-units")
    assert write_step["ok"] is True

    unit_dir = tmp_path / "units"
    assert (unit_dir / service_linux.WEB_SERVICE_NAME).exists()
    assert (unit_dir / service_linux.WATCHDOG_SERVICE_NAME).exists()
    assert (unit_dir / service_linux.WATCHDOG_TIMER_NAME).exists()

    # Every step (even failing ones, e.g. no systemd user session here)
    # is reported -- never silently dropped.
    assert len(steps) >= 5
    for step in steps:
        assert "ok" in step and "step" in step and "detail" in step


def test_install_reports_write_failure_and_stops(tmp_path, monkeypatch):
    bad_dir = tmp_path / "not-writable-parent" / "units"
    monkeypatch.setenv(service_linux.UNIT_DIR_ENV, str(bad_dir))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(service_linux.Path, "mkdir", boom)
    steps = service_linux.install("/opt/crr/bin/crr")
    assert len(steps) == 1
    assert steps[0]["step"] == "write-units"
    assert steps[0]["ok"] is False


def test_uninstall_removes_unit_files(tmp_path, monkeypatch):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    monkeypatch.setenv(service_linux.UNIT_DIR_ENV, str(unit_dir))
    for name in (
        service_linux.WEB_SERVICE_NAME,
        service_linux.WATCHDOG_SERVICE_NAME,
        service_linux.WATCHDOG_TIMER_NAME,
    ):
        (unit_dir / name).write_text("dummy\n")

    service_linux.uninstall()
    for name in (
        service_linux.WEB_SERVICE_NAME,
        service_linux.WATCHDOG_SERVICE_NAME,
        service_linux.WATCHDOG_TIMER_NAME,
    ):
        assert not (unit_dir / name).exists()
