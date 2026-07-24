"""systemd watchdog adapter tests.

Pure unit builders + a PATH resolver + file-writing, all testable without
touching the real user manager. The enable/linger step is returned as data
(argv lists) so it can be asserted without being run — the tests never
call systemctl/loginctl. A gated `systemd-analyze verify` check catches
malformed directives the string assertions would miss.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from crr.adapters import systemd


def test_service_unit_bakes_execstart_path_and_state_home():
    unit = systemd.revive_service_unit(
        crr_bin="/opt/crr/bin/crr",
        path="/opt/crr/bin:/usr/bin:/bin",
        state_home="/home/u/.local/state",
    )
    assert "Type=oneshot" in unit
    assert "ExecStart=/opt/crr/bin/crr revive" in unit
    assert "Environment=PATH=/opt/crr/bin:/usr/bin:/bin" in unit
    # [lesson: interop PATH] generalized — the service can't see the shell's
    # XDG_STATE_HOME, so it must be baked or the watchdog watches the wrong dir.
    assert "Environment=XDG_STATE_HOME=/home/u/.local/state" in unit


def test_timer_unit_uses_the_interval_and_installs_to_timers_target():
    unit = systemd.revive_timer_unit(interval_seconds=30)
    assert "OnUnitActiveSec=30s" in unit
    assert "WantedBy=timers.target" in unit


def test_resolve_service_path_includes_crr_dir_and_system_dirs():
    path, missing = systemd.resolve_service_path("/opt/crr/bin/crr")
    dirs = path.split(":")
    assert "/opt/crr/bin" in dirs          # where crr itself lives
    assert "/usr/bin" in dirs               # standard system dir
    assert isinstance(missing, list)        # any binary that didn't resolve


def test_resolve_service_path_reports_missing_binaries(monkeypatch):
    # If a required binary can't be found, it is reported, not hidden — a
    # silent missing `claude` would make every revival die on exec.
    monkeypatch.setattr(systemd.shutil, "which", lambda name: None)
    _, missing = systemd.resolve_service_path("/opt/crr/bin/crr")
    assert set(missing) == set(systemd.SERVICE_BINARIES)


def test_write_units_creates_both_files(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    svc, timer = systemd.write_units(unit_dir, "SVC-CONTENT", "TIMER-CONTENT")
    assert svc.name == systemd.SERVICE_NAME and svc.read_text() == "SVC-CONTENT"
    assert timer.name == systemd.TIMER_NAME and timer.read_text() == "TIMER-CONTENT"


def test_enable_commands_are_daemon_reload_enable_and_linger():
    cmds = systemd.enable_commands()
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert any("enable" in c and systemd.TIMER_NAME in c for c in cmds)
    assert any(c[0] == "loginctl" and "enable-linger" in c for c in cmds)


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not available")
def test_generated_units_pass_systemd_analyze_verify(tmp_path):
    # The systemd analogue of the node --check page gate: verify the real
    # tool accepts the generated units. Uses a real existing ExecStart binary
    # so verify's existence checks pass.
    crr_bin = shutil.which("true")  # a binary that exists
    path, _ = systemd.resolve_service_path(crr_bin)
    svc = systemd.revive_service_unit(crr_bin=crr_bin, path=path, state_home=str(tmp_path))
    timer = systemd.revive_timer_unit(interval_seconds=30)
    svc_path, timer_path = systemd.write_units(tmp_path, svc, timer)
    for unit in (svc_path, timer_path):
        result = subprocess.run(
            ["systemd-analyze", "verify", str(unit)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"{unit.name}: {result.stderr}"
