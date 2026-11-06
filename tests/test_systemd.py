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

import inspect

from crr.adapters import systemd
from crr.core.config import DEFAULTS


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


@pytest.mark.skipif(
    os.name == "nt",
    reason="asserts on POSIX path literals; os.path.abspath composes "
           "them under ntpath here, so they acquire a drive letter "
           "and the assertion measures path semantics rather than "
           "crr. The unit this PATH goes into targets Linux/macOS",
)
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


@pytest.mark.skipif(
    os.name == "nt",
    reason="asserts on POSIX path literals; os.path.abspath composes "
           "them under ntpath here, so they acquire a drive letter "
           "and the assertion measures path semantics rather than "
           "crr. The unit this PATH goes into targets Linux/macOS",
)
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


def test_web_service_unit_restart_spares_sessions_it_spawned():
    # #140: a dashboard Reopen/Restore starts the tmux server from the web
    # process, so it lives in crr-web's cgroup. Under the default
    # KillMode=control-group any web restart (a deploy, a crash-restart)
    # killed those sessions — one was lost mid-turn deploying #139.
    unit = systemd.web_service_unit(
        crr_bin="/opt/crr/bin/crr", path="/usr/bin", state_home="/s", port=8377)
    assert "KillMode=process" in unit


def test_web_service_unit_honors_configured_restart_seconds():
    # F7: RestartSec was baked at 2, unconfigurable — now threaded like the
    # watchdog interval (default preserved for callers that don't pass it).
    unit = systemd.web_service_unit(
        crr_bin="/opt/crr/bin/crr",
        path="/opt/crr/bin:/usr/bin",
        state_home="/home/u/.local/state",
        port=8377,
        restart_seconds=9,
    )
    assert "RestartSec=9" in unit


def test_web_service_unit_restart_seconds_default_is_the_named_config_default():
    # Finding 5 (re-audit): the literal `2` used to be a second, undeduped
    # copy of `DEFAULTS["web_restart_seconds"]` — this pins the signature
    # default to the named config default so they can't drift apart again.
    default = inspect.signature(systemd.web_service_unit).parameters["restart_seconds"].default
    assert default == DEFAULTS["web_restart_seconds"]


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
        ["systemctl", "--user", "disable", "--now", systemd.AWAKE_SERVICE_NAME],
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
        systemd.AWAKE_SERVICE_NAME: systemd.awake_service_unit(crr_bin=crr_bin, path=path, state_home=str(tmp_path)),
    }
    for unit in systemd.write_units(tmp_path, units):
        result = subprocess.run(
            ["systemd-analyze", "verify", str(unit)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"{unit.name}: {result.stderr}"


def test_generated_units_carry_a_version_stamp():
    """[audit P7] every stored crr artifact stamps a version; generated
    install artifacts must too, so a stale unit is distinguishable later."""
    for text in (
        systemd.revive_service_unit("/x/crr", "/bin", "/state"),
        systemd.revive_timer_unit(30),
        systemd.web_service_unit("/x/crr", "/bin", "/state", 8377),
        systemd.awake_service_unit("/x/crr", "/bin", "/state"),
    ):
        assert text.startswith("# generated by crr "), text[:60]
        assert "config-defaults v" in text.splitlines()[0]


def test_awake_unit_is_a_long_running_service_that_restarts():
    unit = systemd.awake_service_unit("/opt/crr/bin/crr", "/usr/bin", "/home/u/.local/state")
    assert "Type=simple" in unit
    assert "ExecStart=/opt/crr/bin/crr awake" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_awake_unit_bakes_the_state_dir_like_the_other_units():
    unit = systemd.awake_service_unit("/opt/crr/bin/crr", "/usr/bin", "/home/u/.local/state")
    assert "Environment=XDG_STATE_HOME=/home/u/.local/state" in unit
    assert "Environment=PATH=/usr/bin" in unit


def test_awake_unit_stops_with_a_signal_the_loop_can_catch():
    # The loop releases the hold in a finally block on SIGTERM. A unit
    # that killed with SIGKILL would skip that, leaving the release to the
    # holder's fallback.
    unit = systemd.awake_service_unit("/opt/crr/bin/crr", "/usr/bin", "/s")
    assert "KillSignal=SIGKILL" not in unit


def test_awake_is_enabled_and_disabled_with_the_rest():
    assert any(systemd.AWAKE_SERVICE_NAME in c for c in
               [" ".join(x) for x in systemd.enable_commands()])
    assert any(systemd.AWAKE_SERVICE_NAME in c for c in
               [" ".join(x) for x in systemd.disable_commands()])


def test_awake_can_be_stopped_on_its_own():
    assert systemd.stop_awake_command() == [
        "systemctl", "--user", "stop", systemd.AWAKE_SERVICE_NAME]


# --- user-manager fallback unit (2026-09-08: WSL logind/linger race) ------
#
# logind is supposed to start user@<uid> at boot for lingering users and
# silently did not at the 2026-09-05 boot — three days with the dashboard
# and watchdog down. The fallback is a SYSTEM-level oneshot that starts the
# user manager with the same ordering user@.service itself uses.

def test_user_manager_fallback_unit_content():
    text = systemd.user_manager_fallback_unit(1000)
    assert "ExecStart=/usr/bin/systemctl start user@1000.service" in text
    assert "Type=oneshot" in text
    assert "RemainAfterExit=yes" in text
    assert "WantedBy=multi-user.target" in text
    # Same ordering user@.service itself carries — never earlier.
    assert "After=systemd-user-sessions.service systemd-logind.service" in text


def test_user_manager_fallback_unit_name_is_pinned():
    assert systemd.USER_MANAGER_FALLBACK_UNIT == "crr-user-manager.service"


def test_user_manager_fallback_state_tri_state(monkeypatch):
    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(systemd.subprocess, "run", lambda *a, **k: R(0, "enabled\n"))
    assert systemd.user_manager_fallback_state(2.0) == "installed"
    monkeypatch.setattr(systemd.subprocess, "run", lambda *a, **k: R(1, "not-found\n"))
    assert systemd.user_manager_fallback_state(2.0) == "missing"
    monkeypatch.setattr(systemd.subprocess, "run", lambda *a, **k: R(1, "disabled\n"))
    # Present but not enabled will not fire at boot — that is missing, not installed.
    assert systemd.user_manager_fallback_state(2.0) == "missing"

    def boom(*a, **k):
        raise OSError("no systemctl")

    monkeypatch.setattr(systemd.subprocess, "run", boom)
    assert systemd.user_manager_fallback_state(2.0) == "unknown"
    monkeypatch.setattr(systemd.shutil, "which", lambda _: None)
    assert systemd.user_manager_fallback_state(2.0) == "unknown"


def test_user_manager_fallback_install_argv_runs_root_via_wsl_interop():
    argv = systemd.user_manager_fallback_install_argv(1000)
    assert argv[0].endswith("wsl.exe")
    assert argv[1:4] == ["-u", "root", "--"]
    script = argv[-1]
    assert "crr-user-manager.service" in script
    assert "daemon-reload" in script
    assert "enable" in script
    assert "user@1000.service" in script


def test_user_manager_fallback_uninstall_argv_disables_and_removes():
    argv = systemd.user_manager_fallback_uninstall_argv()
    assert argv[0].endswith("wsl.exe")
    assert argv[1:4] == ["-u", "root", "--"]
    script = argv[-1]
    assert "disable" in script and "crr-user-manager.service" in script
    assert "rm -f" in script and "daemon-reload" in script


# --- PATH order (#165) ----------------------------------------------------------------

def _install(directory, *names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        binary = directory / name
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    return directory


@pytest.mark.skipif(os.name == "nt", reason="POSIX PATH semantics; the unit targets Linux")
def test_every_service_binary_resolves_to_what_the_shell_found(tmp_path, monkeypatch):
    # tmux and ps live in /usr/bin, claude in ~/.local/bin, and an old claude
    # sits in /usr/bin too. Adding dirs per binary put /usr/bin first, so the
    # service ran the old claude while the shell ran the new one.
    local = _install(tmp_path / "home" / ".local" / "bin", "claude")
    system = _install(tmp_path / "usr" / "bin", "tmux", "ps", "claude")
    monkeypatch.setenv("PATH", f"{local}{os.pathsep}{system}")

    path, missing = systemd.resolve_service_path("/opt/crr/bin/crr")

    assert missing == []
    for name in systemd.SERVICE_BINARIES:
        assert shutil.which(name, path=path) == shutil.which(name), name


@pytest.mark.skipif(os.name == "nt", reason="POSIX PATH semantics")
def test_crrs_own_dir_never_shadows_the_shells_claude(tmp_path, monkeypatch):
    # crr installed beside an old claude (npm's /usr/local prefix) must not
    # put that claude ahead of the one the shell runs.
    local = _install(tmp_path / "home" / ".local" / "bin", "claude")
    own = _install(tmp_path / "usr" / "local" / "bin", "crr", "claude")
    system = _install(tmp_path / "usr" / "bin", "tmux", "ps")
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, (local, own, system))))

    path, _ = systemd.resolve_service_path(str(own / "crr"))

    for name in systemd.SERVICE_BINARIES:
        assert shutil.which(name, path=path) == shutil.which(name), name
    assert str(own) in path.split(os.pathsep)


# --- unit_states: `crr service status`' probe of crr's own units (#161) ---

_SHOW_OUT = (
    "Id=crr-revive.timer\nLoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
    "\n"
    "Id=crr-web.service\nLoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"
    "\n"
    "Id=crr-awake.service\nLoadState=not-found\nActiveState=inactive\nUnitFileState=\n"
)


def _fake_systemctl(monkeypatch, *, stdout="", returncode=0, exc=None, calls=None):
    monkeypatch.setattr(systemd.shutil, "which", lambda name: "/usr/bin/systemctl")

    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)


def test_crr_units_names_the_three_units():
    assert systemd.CRR_UNITS == (
        ("watchdog", systemd.TIMER_NAME),
        ("dashboard", systemd.WEB_SERVICE_NAME),
        ("keep-awake", systemd.AWAKE_SERVICE_NAME),
    )


def test_unit_states_parses_systemctl_show(monkeypatch):
    calls = []
    _fake_systemctl(monkeypatch, stdout=_SHOW_OUT, calls=calls)
    states = systemd.unit_states(3.0)
    assert [tuple(s) for s in states] == [
        ("watchdog", "crr-revive.timer", True, True, True),
        ("dashboard", "crr-web.service", True, False, False),
        ("keep-awake", "crr-awake.service", False, False, False),
    ]
    argv, kwargs = calls[0]
    assert argv[:3] == ["systemctl", "--user", "show"]
    assert argv[argv.index("-p") + 1] == "Id,LoadState,UnitFileState,ActiveState"
    for _, unit in systemd.CRR_UNITS:
        assert unit in argv
    assert kwargs["timeout"] == 3.0


@pytest.mark.parametrize("kwargs", [
    {"returncode": 1, "stdout": ""},                       # no user bus
    {"stdout": ""},                                        # answered nothing
    {"stdout": "Id=crr-revive.timer\nLoadState=loaded\n"},  # missing units
    {"exc": subprocess.TimeoutExpired("systemctl", 3)},
    {"exc": FileNotFoundError("systemctl")},
    {"exc": PermissionError("systemctl")},
])
def test_unit_states_is_none_when_the_user_manager_cannot_answer(monkeypatch, kwargs):
    _fake_systemctl(monkeypatch, **kwargs)
    assert systemd.unit_states(3.0) is None


def test_unit_states_is_none_without_systemctl(monkeypatch):
    monkeypatch.setattr(systemd.shutil, "which", lambda name: None)
    assert systemd.unit_states(3.0) is None


# --- the port the installed dashboard really runs on (#161) ---

@pytest.mark.parametrize("exec_start, port", [
    ("/opt/crr/bin/crr web --port 8377", 8377),
    ("/home/u/my bin/crr web --port 9000", 9000),
    ("/opt/crr/bin/crr web --port=9001", 9001),
    ("/opt/crr/bin/crr web", None),
    ("/opt/crr/bin/crr web --port abc", None),
    ("/opt/crr/bin/crr web --port 99999", None),
])
def test_parse_web_port_reads_execstart(exec_start, port):
    unit = f"[Service]\nType=simple\nExecStart={exec_start}\nRestart=on-failure\n"
    assert systemd.parse_web_port(unit) == port


def test_parse_web_port_round_trips_the_unit_the_adapter_writes():
    unit = systemd.web_service_unit("/opt/crr/bin/crr", "/usr/bin", "/state", 9123)
    assert systemd.parse_web_port(unit) == 9123


def test_installed_web_port_reads_the_unit_file(tmp_path):
    assert systemd.installed_web_port(tmp_path) is None          # nothing installed
    systemd.write_units(systemd.unit_dir(tmp_path), {
        systemd.WEB_SERVICE_NAME: systemd.web_service_unit("/x/crr", "/bin", "/s", 8444)})
    assert systemd.installed_web_port(tmp_path) == 8444


# --- crr-web as the user manager has it loaded (#161, C1) -------------------

_WEB_SHOW_OUT = "MainPID=4242\nKillMode=process\nLoadState=loaded\nActiveState=active\n"


@pytest.mark.real_service_probes
def test_web_unit_props_reads_the_loaded_values(monkeypatch):
    calls = []
    _fake_systemctl(monkeypatch, stdout=_WEB_SHOW_OUT, calls=calls)
    assert systemd.web_unit_props(3.0) == {
        "MainPID": "4242", "KillMode": "process",
        "LoadState": "loaded", "ActiveState": "active"}
    argv, kwargs = calls[0]
    assert argv == ["systemctl", "--user", "show", "crr-web.service",
                    "-p", "LoadState,ActiveState,KillMode,MainPID"]
    assert kwargs["timeout"] == 3.0


@pytest.mark.real_service_probes
@pytest.mark.parametrize("kwargs", [
    {"returncode": 1, "stdout": ""},                       # no user bus
    {"stdout": ""},                                        # answered nothing
    {"exc": subprocess.TimeoutExpired("systemctl", 3)},
    {"exc": FileNotFoundError("systemctl")},
])
def test_web_unit_props_is_none_when_the_user_manager_cannot_answer(monkeypatch, kwargs):
    _fake_systemctl(monkeypatch, **kwargs)
    assert systemd.web_unit_props(3.0) is None


@pytest.mark.real_service_probes
def test_web_unit_props_is_none_without_systemctl(monkeypatch):
    monkeypatch.setattr(systemd.shutil, "which", lambda name: None)
    assert systemd.web_unit_props(3.0) is None


@pytest.mark.parametrize("props, kill_mode", [
    ({"LoadState": "loaded", "KillMode": "process"}, "process"),
    ({"LoadState": "loaded", "KillMode": "control-group"}, "control-group"),
    # A unit systemd could not find still reports the default control-group;
    # that says nothing about a unit file, so it is unknown.
    ({"LoadState": "not-found", "KillMode": "control-group"}, None),
    ({"LoadState": "loaded", "KillMode": ""}, None),
    ({}, None),
    (None, None),
])
def test_loaded_kill_mode(props, kill_mode):
    assert systemd.loaded_kill_mode(props) == kill_mode


# --- the port the RUNNING dashboard listens on (#161, I5) -------------------

@pytest.mark.parametrize("raw, port", [
    (b"/app/bin/python3\0/app/bin/crr\0web\0--port\08377\0", 8377),
    (b"/app/bin/crr\0web\0--port=9001\0", 9001),
    (b"/home/u/my bin/crr\0web\0--port\09000\0", 9000),   # spaces survive NULs
    (b"/app/bin/crr\0web\0", None),                          # no --port
    (b"/app/bin/crr\0revive\0--port\08377\0", None),       # not `crr web`
    (b"/app/bin/crr\0web\0--port\0abc\0", None),
    (b"/app/bin/crr\0web\0--port\099999\0", None),
    (b"", None),
])
def test_parse_cmdline_port(raw, port):
    assert systemd.parse_cmdline_port(raw) == port


@pytest.mark.parametrize("props, pid", [
    ({"ActiveState": "active", "MainPID": "4242"}, 4242),
    ({"ActiveState": "inactive", "MainPID": "4242"}, None),
    ({"ActiveState": "active", "MainPID": "0"}, None),       # no main process
    ({"ActiveState": "active", "MainPID": "x"}, None),
    ({"ActiveState": "active"}, None),
    (None, None),
])
def test_running_main_pid(props, pid):
    assert systemd.running_main_pid(props) == pid


def test_running_web_port_reads_the_main_pids_cmdline(tmp_path):
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "cmdline").write_bytes(b"/app/crr\0web\0--port\08377\0")
    active = {"ActiveState": "active", "MainPID": "4242"}
    assert systemd.running_web_port(active, proc_root=tmp_path) == 8377
    assert systemd.running_web_port({**active, "MainPID": "5"}, proc_root=tmp_path) is None
    assert systemd.running_web_port({**active, "ActiveState": "inactive"},
                                    proc_root=tmp_path) is None
    assert systemd.running_web_port(None, proc_root=tmp_path) is None


def test_running_web_port_defaults_to_the_module_proc_root(tmp_path, monkeypatch):
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "cmdline").write_bytes(b"crr\0web\0--port\09100\0")
    monkeypatch.setattr(systemd, "PROC_ROOT", tmp_path)
    assert systemd.running_web_port({"ActiveState": "active", "MainPID": "7"}) == 9100


# --- crr service start / stop (#161) ----------------------------------------

def test_start_argv():
    assert systemd.start_argv() == [
        "systemctl", "--user", "start",
        "crr-revive.timer", "crr-web.service", "crr-awake.service"]


def test_stop_argv():
    assert systemd.stop_argv() == [
        "systemctl", "--user", "stop",
        "crr-revive.timer", "crr-web.service", "crr-awake.service"]


# --- crr logs (#161) ----------------------------------------------------------

def test_log_units_map_the_three_service_names():
    assert systemd.LOG_UNITS == {
        "revive": "crr-revive.service",
        "web": "crr-web.service",
        "awake": "crr-awake.service",
    }


def test_journal_argv():
    assert systemd.journal_argv("crr-web.service", 200, False) == [
        "journalctl", "--user", "-u", "crr-web.service", "-n", "200",
        "--no-pager", "-o", "short-iso"]


def test_journal_argv_follow_appends_f():
    assert systemd.journal_argv("crr-revive.service", 5, True) == [
        "journalctl", "--user", "-u", "crr-revive.service", "-n", "5",
        "--no-pager", "-o", "short-iso", "-f"]
