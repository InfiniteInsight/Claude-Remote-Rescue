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
    # KillMode=process is required: without it the oneshot's cgroup cleanup
    # kills the detached tmux server it just spawned, so every revived session
    # dies the instant the watchdog exits (found on real systemd, hardware test).
    assert "KillMode=process" in unit
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


def test_resolve_service_path_includes_extra_binaries_dir(monkeypatch):
    # [live bug, 2026-07-31] WSL tab spawning shells out to wt.exe/wsl.exe,
    # which live under Windows dirs the baked SERVICE_BINARIES loop never
    # sees — a caller-supplied extra must land in the PATH just like the
    # fixed binaries do.
    def fake_which(name):
        if name == "wt.exe":
            return "/mnt/c/Users/Infin/AppData/Local/Microsoft/WindowsApps/wt.exe"
        return f"/usr/bin/{name}"

    monkeypatch.setattr(systemd.shutil, "which", fake_which)
    path, missing = systemd.resolve_service_path(
        "/opt/crr/bin/crr", extra_binaries=("wt.exe",)
    )
    assert "/mnt/c/Users/Infin/AppData/Local/Microsoft/WindowsApps" in path.split(":")
    assert missing == []


def test_resolve_service_path_reports_unresolved_extra_binaries(monkeypatch):
    def fake_which(name):
        return f"/usr/bin/{name}" if name in systemd.SERVICE_BINARIES else None

    monkeypatch.setattr(systemd.shutil, "which", fake_which)
    _, missing = systemd.resolve_service_path(
        "/opt/crr/bin/crr", extra_binaries=("wt.exe", "wsl.exe")
    )
    assert set(missing) == {"wt.exe", "wsl.exe"}


def test_resolve_service_path_extra_binaries_defaults_to_empty():
    # Non-WSL callers pass nothing; SERVICE_BINARIES-only behavior unchanged.
    path, missing = systemd.resolve_service_path("/opt/crr/bin/crr")
    assert isinstance(missing, list)
    assert "wt.exe" not in path


def test_web_service_unit_runs_crr_web_and_stays_up():
    unit = systemd.web_service_unit(
        crr_bin="/opt/crr/bin/crr",
        path="/opt/crr/bin:/usr/bin",
        state_home="/home/u/.local/state",
        port=8377,
    )
    assert "ExecStart=/opt/crr/bin/crr web --port 8377" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit  # comes back at (re)boot with linger
    assert "Environment=XDG_STATE_HOME=/home/u/.local/state" in unit
    # No WSL_DISTRO_NAME baked when not given — non-WSL hosts stay unaffected.
    assert "WSL_DISTRO_NAME" not in unit


def test_web_service_unit_bakes_wsl_distro_name_when_given():
    # [live bug, 2026-07-31] WindowsTerminalSpawner reads WSL_DISTRO_NAME
    # from os.environ at call time (cli.py's _select_tab_spawner) to pass
    # `wsl.exe --distribution <name>` — but a systemd user service does not
    # inherit the interactive shell's exported vars any more than it
    # inherits XDG_STATE_HOME, so on a multi-distro host the tab would
    # silently open in the *default* distro instead of this one unless it
    # is baked into the unit the same way.
    unit = systemd.web_service_unit(
        crr_bin="/opt/crr/bin/crr",
        path="/opt/crr/bin:/usr/bin",
        state_home="/home/u/.local/state",
        port=8377,
        wsl_distro="Ubuntu",
    )
    assert "Environment=WSL_DISTRO_NAME=Ubuntu" in unit


def test_write_units_writes_all_named_files(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    paths = systemd.write_units(unit_dir, {
        systemd.SERVICE_NAME: "SVC", systemd.TIMER_NAME: "TIMER", systemd.WEB_SERVICE_NAME: "WEB",
    })
    names = {p.name for p in paths}
    assert names == {systemd.SERVICE_NAME, systemd.TIMER_NAME, systemd.WEB_SERVICE_NAME}
    assert (unit_dir / systemd.WEB_SERVICE_NAME).read_text() == "WEB"


def test_enable_commands_enable_timer_web_and_linger():
    cmds = systemd.enable_commands()
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert any("enable" in c and systemd.TIMER_NAME in c for c in cmds)
    assert any("enable" in c and systemd.WEB_SERVICE_NAME in c for c in cmds)
    assert any(c[0] == "loginctl" and "enable-linger" in c for c in cmds)


def test_critical_enable_commands_excludes_linger():
    # [Task 7] linger is split out so the CLI can treat its failure as a
    # warning (WSL2's dbus quirk) while daemon-reload/enable failures stay
    # hard failures — critical_enable_commands() must carry only the latter.
    cmds = systemd.critical_enable_commands()
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert any("enable" in c and systemd.TIMER_NAME in c for c in cmds)
    assert any("enable" in c and systemd.WEB_SERVICE_NAME in c for c in cmds)
    assert not any(c[0] == "loginctl" for c in cmds)


def test_linger_command_is_loginctl_enable_linger():
    assert systemd.linger_command() == ["loginctl", "enable-linger"]


def test_enable_commands_is_critical_commands_plus_linger():
    # print mode (`enable_commands()`) must stay unchanged: all four, linger last.
    assert systemd.enable_commands() == systemd.critical_enable_commands() + [
        systemd.linger_command()
    ]


def test_disable_commands_mirror_enable():
    assert systemd.disable_commands() == [
        ["systemctl", "--user", "disable", "--now", systemd.TIMER_NAME],
        ["systemctl", "--user", "disable", "--now", systemd.WEB_SERVICE_NAME],
        ["systemctl", "--user", "daemon-reload"],
    ]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not available")
def test_generated_units_pass_systemd_analyze_verify(tmp_path):
    # The systemd analogue of the node --check page gate: verify the real
    # tool accepts the generated units. Uses a real existing ExecStart binary
    # so verify's existence checks pass.
    crr_bin = shutil.which("true")  # a binary that exists
    path, _ = systemd.resolve_service_path(crr_bin)
    units = {
        systemd.SERVICE_NAME: systemd.revive_service_unit(crr_bin=crr_bin, path=path, state_home=str(tmp_path)),
        systemd.TIMER_NAME: systemd.revive_timer_unit(interval_seconds=30),
        systemd.WEB_SERVICE_NAME: systemd.web_service_unit(crr_bin=crr_bin, path=path, state_home=str(tmp_path), port=8377),
    }
    for unit in systemd.write_units(tmp_path, units):
        result = subprocess.run(
            ["systemd-analyze", "verify", str(unit)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"{unit.name}: {result.stderr}"
