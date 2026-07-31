"""CLI integration tests (the composition root wired end-to-end).

`config --effective` is pure and runs anywhere. `status --json` exercises
the real adapters (boot identity, process probe) and so is gated to Linux
— the Phase 1 headless target; DESIGN.md gates platform adapter tests
this way.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from crr import cli
from crr.adapters import boot_identity, state_dir
from crr.core import config as cfg
from crr.core import contracts
from crr.core.archive import ArchiveStore
from crr.core.journal import JournalStore, new_entry


@pytest.mark.skipif(platform.system() != "Linux", reason="journald source is Linux-selected")
def test_diagnose_degrades_cleanly_when_journald_absent(monkeypatch, capsys):
    # No journald on native (non-WSL) Linux => a valid payload with every
    # source marked degraded, never a crash or a silently-empty result. (On
    # macOS the log+pmset source is selected; under WSL, the WinEvent+OOM one.)
    from crr.adapters import diagnostics as diag_source
    monkeypatch.setattr(diag_source, "available", lambda: False)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)  # pin native Linux
    rc = cli.main(["diagnose", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    contracts.validate_diagnostics_payload(payload)
    assert payload["source"] == "journald"
    assert set(payload["degraded"]) == {"boots", "prev_boot_errors", "host_events"}


def test_select_diag_source_uses_windows_wsl_source_when_journald_absent(monkeypatch):
    # WSL without journald -> the WinEvent+OOM source; with journald present
    # (or native Linux) -> journald.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.diag_source, "available", lambda: False)
    assert cli._select_diag_source() is cli.diagnostics_windows
    monkeypatch.setattr(cli.diag_source, "available", lambda: True)
    assert cli._select_diag_source() is cli.diag_source
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.diag_source, "available", lambda: False)
    assert cli._select_diag_source() is cli.diag_source  # native Linux stays journald


def test_diagnose_selects_macos_source_and_degrades_when_tools_absent(monkeypatch, capsys):
    # Force the macOS branch on any host: the composition root picks the
    # log+pmset source, which degrades every field when its tools are absent.
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.diagnostics_macos, "available", lambda: False)
    rc = cli.main(["diagnose", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    contracts.validate_diagnostics_payload(payload)
    assert payload["source"] == "log+pmset"
    assert set(payload["degraded"]) == {"boots", "prev_boot_errors", "host_events"}


@pytest.mark.skipif(
    platform.system() not in ("Linux", "Darwin") or shutil.which("journalctl") is None,
    reason="needs Linux journald",
)
def test_diagnose_emits_contract_valid_payload_from_journald(capsys):
    rc = cli.main(["diagnose", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    contracts.validate_diagnostics_payload(payload)
    assert payload["source"] == "journald"
    assert isinstance(payload["boots"], list)


def test_web_server_serves_sessions_and_enforces_host(tmp_path):
    # Real socket + real HTTP, fake provider (no platform coupling). Proves
    # the http.server adapter wires to the pure handler end to end.
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    payload = {"contract": 1, "sessions": [{"pid": 7, "state": "ghost"}]}
    handler = cli.make_web_handler(lambda: payload, {"localhost", "127.0.0.1"}, (".ts.net",))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # Allowed host -> 200 + payload + no-store.
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sessions",
                                     headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            assert r.headers["Cache-Control"] == "no-store"
            assert json.loads(r.read())["sessions"][0]["pid"] == 7

        # Disallowed Host header -> 403 (DNS-rebinding defense).
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sessions",
                                     headers={"Host": "evil.com"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_systemd_print_emits_both_units_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    rc = cli.main(["systemd"])  # default: print, no --install
    out = capsys.readouterr().out
    assert rc == 0
    assert "crr-revive.service" in out and "crr-revive.timer" in out
    assert "ExecStart=/opt/crr/bin/crr revive" in out
    # XDG_STATE_HOME baked as the parent of the resolved state dir.
    assert f"Environment=XDG_STATE_HOME={tmp_path / 'state'}" in out
    # Print mode must not touch the user's systemd dir.
    assert not (tmp_path / ".config" / "systemd").exists()


def test_systemd_print_bakes_wt_exe_dir_into_path_on_wsl(tmp_path, monkeypatch, capsys):
    # [live bug, 2026-07-31] wt.exe/wsl.exe live under Windows dirs that the
    # baked SERVICE_BINARIES loop never sees, so the deployed service PATH
    # could not resolve them and the dashboard's tab spawner reported
    # unavailable. On WSL, the unit's PATH must include their dirs.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)

    def fake_which(name):
        if name == "wt.exe":
            return "/mnt/c/Users/Infin/AppData/Local/Microsoft/WindowsApps/wt.exe"
        if name == "wsl.exe":
            return "/mnt/c/windows/system32/wsl.exe"
        return f"/usr/bin/{name}"

    monkeypatch.setattr(cli.systemd.shutil, "which", fake_which)
    rc = cli.main(["systemd"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "/mnt/c/Users/Infin/AppData/Local/Microsoft/WindowsApps" in out
    assert "/mnt/c/windows/system32" in out


def test_systemd_print_does_not_consult_wt_exe_when_not_wsl(tmp_path, monkeypatch, capsys):
    # Non-WSL Linux must not warn about (or even look for) wt.exe/wsl.exe —
    # the extras are caller-supplied, SERVICE_BINARIES itself is untouched.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    consulted = []

    def fake_which(name):
        consulted.append(name)
        return f"/usr/bin/{name}" if name in cli.systemd.SERVICE_BINARIES else None

    monkeypatch.setattr(cli.systemd.shutil, "which", fake_which)
    rc = cli.main(["systemd"])
    assert rc == 0
    assert "wt.exe" not in consulted and "wsl.exe" not in consulted


def test_systemd_print_bakes_wsl_distro_name_when_wsl(tmp_path, monkeypatch, capsys):
    # A systemd user service does not inherit the interactive shell's
    # WSL_DISTRO_NAME any more than XDG_STATE_HOME — without baking it,
    # WindowsTerminalSpawner (read at request time via os.environ) would
    # open tabs in the host's default distro instead of this one.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(cli.systemd.shutil, "which", lambda name: f"/usr/bin/{name}")
    rc = cli.main(["systemd"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Environment=WSL_DISTRO_NAME=Ubuntu" in out


def test_systemd_print_warns_accurately_when_only_tab_spawn_binaries_missing(
    tmp_path, monkeypatch, capsys
):
    # wt.exe/wsl.exe missing only degrades tab spawning (Untrack/Un-tmux/Reopen),
    # never revival — the pre-existing "revived sessions will fail on exec"
    # wording would overclaim if reused verbatim for these extras.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)

    def fake_which(name):
        return None if name in ("wt.exe", "wsl.exe") else f"/usr/bin/{name}"

    monkeypatch.setattr(cli.systemd.shutil, "which", fake_which)
    rc = cli.main(["systemd"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "wt.exe" in err and "wsl.exe" in err
    assert "revived sessions will fail on exec" not in err
    assert "tab" in err.lower()


def test_systemd_print_omits_wsl_distro_name_when_not_wsl(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")  # e.g. stale env from another host
    monkeypatch.setattr(cli.systemd.shutil, "which", lambda name: f"/usr/bin/{name}")
    rc = cli.main(["systemd"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WSL_DISTRO_NAME" not in out


def test_launchd_print_emits_both_agents_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    rc = cli.main(["launchd"])  # default: print, no --install
    out = capsys.readouterr().out
    assert rc == 0
    assert "com.claude-remote-rescue.revive.plist" in out
    assert "com.claude-remote-rescue.web.plist" in out
    assert "/opt/crr/bin/crr" in out  # baked ProgramArguments
    assert "launchctl" in out and "load" in out  # printed enable guidance
    # Print mode must not touch the user's LaunchAgents dir.
    assert not (tmp_path / "Library" / "LaunchAgents").exists()


def test_schtasks_print_emits_both_tasks_and_runs_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/usr/bin/crr")
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("print must not run schtasks")))
    rc = cli.main(["schtasks", "--port", "8378"])  # default: print
    out = capsys.readouterr().out
    assert rc == 0
    assert cli.scheduled_task.REVIVE_TASK in out and cli.scheduled_task.WEB_TASK in out
    assert "wsl.exe" in out and "/usr/bin/crr" in out
    assert "web --port 8378" in out
    assert "schtasks --install" in out  # printed install guidance


def test_systemd_install_failure_propagates(tmp_path, monkeypatch, capsys):
    """[bug 2026-07-29 / DESIGN lesson] a failed systemctl must not print the
    success line nor exit 0 — a swallowed exit code is a green checkmark."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=1)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.main(["systemd", "--install", "--crr-bin", "/usr/bin/crr"])
    out, err = capsys.readouterr()
    assert rc != 0
    assert "installed watchdog" not in out          # no success claim
    assert "exited 1" in err or "failed" in err     # failure surfaced
    assert calls                                     # commands were attempted


def test_systemd_install_success_still_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, returncode=0))
    rc = cli.main(["systemd", "--install", "--crr-bin", "/usr/bin/crr"])
    assert rc == 0
    assert "installed watchdog" in capsys.readouterr().out


def test_launchd_install_failure_propagates(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, returncode=1))
    rc = cli.main(["launchd", "--install", "--crr-bin", "/usr/bin/crr"])
    assert rc != 0
    assert "installed watchdog" not in capsys.readouterr().out


def test_schtasks_install_refuses_without_schtasks_exe(monkeypatch, capsys):
    """Off Windows/WSL the old code 'created' tasks it never created."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("refusal must not run schtasks")))
    rc = cli.main(["schtasks", "--install", "--crr-bin", "/usr/bin/crr"])
    assert rc != 0
    assert "created watchdog" not in capsys.readouterr().out


def test_restore_is_an_alias_for_reopen(monkeypatch):
    """DESIGN names the op 'reopen/restore'; both must parse."""
    seen = {}

    def fake_reopen(args):
        seen["pid"] = args.pid
        return 0

    monkeypatch.setattr(cli, "_cmd_reopen", fake_reopen)
    assert cli.main(["restore", "--pid", "424242"]) == 0
    assert seen["pid"] == 424242
    # The primary name must still route the same way (aliases=[...] must not
    # have displaced "reopen" itself).
    assert cli.main(["reopen", "--pid", "111111"]) == 0
    assert seen["pid"] == 111111


def test_systemd_install_and_uninstall_together_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run anything")))
    rc = cli.main(["systemd", "--install", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc == 2


def test_systemd_uninstall_disables_and_removes_units(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ud = tmp_path / ".config" / "systemd" / "user"
    ud.mkdir(parents=True)
    for name in (cli.systemd.SERVICE_NAME, cli.systemd.TIMER_NAME, cli.systemd.WEB_SERVICE_NAME):
        (ud / name).write_text("x")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    rc = cli.main(["systemd", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc == 0
    assert ["systemctl", "--user", "disable", "--now", cli.systemd.TIMER_NAME] in ran
    assert not any((ud / n).exists() for n in
                   (cli.systemd.SERVICE_NAME, cli.systemd.TIMER_NAME, cli.systemd.WEB_SERVICE_NAME))


def test_systemd_uninstall_failure_propagates(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1))
    assert cli.main(["systemd", "--uninstall", "--crr-bin", "/usr/bin/crr"]) != 0


def test_launchd_install_and_uninstall_together_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run anything")))
    rc = cli.main(["launchd", "--install", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc == 2


def test_launchd_uninstall_unloads_before_removing_plists(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ad = tmp_path / "Library" / "LaunchAgents"
    ad.mkdir(parents=True)
    (ad / cli.launchd.REVIVE_PLIST).write_text("x")
    (ad / cli.launchd.WEB_PLIST).write_text("x")
    seen_at_unload = {}

    def fake_run(cmd, **k):
        if cmd[:2] == ["launchctl", "unload"]:
            # The plist named in the unload command must still exist —
            # launchctl needs it present to unload.
            plist_path = Path(cmd[-1])
            seen_at_unload[str(plist_path)] = plist_path.exists()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.main(["launchd", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc == 0
    assert seen_at_unload[str(ad / cli.launchd.REVIVE_PLIST)] is True
    assert seen_at_unload[str(ad / cli.launchd.WEB_PLIST)] is True
    assert not (ad / cli.launchd.REVIVE_PLIST).exists()
    assert not (ad / cli.launchd.WEB_PLIST).exists()


def test_launchd_uninstall_failure_propagates(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1))
    assert cli.main(["launchd", "--uninstall", "--crr-bin", "/usr/bin/crr"]) != 0


def test_schtasks_install_and_uninstall_together_errors(monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run anything")))
    rc = cli.main(["schtasks", "--install", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc == 2


def test_schtasks_uninstall_refuses_without_schtasks_exe(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("refusal must not run schtasks")))
    rc = cli.main(["schtasks", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc != 0
    assert "removed watchdog" not in capsys.readouterr().out


def test_schtasks_uninstall_deletes_tasks(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/mnt/c/Windows/System32/schtasks.exe")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    rc = cli.main(["schtasks", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc == 0
    assert ran == cli.scheduled_task.delete_task_commands()
    assert "removed watchdog" in capsys.readouterr().out


def test_schtasks_uninstall_failure_propagates(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/mnt/c/Windows/System32/schtasks.exe")
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1))
    rc = cli.main(["schtasks", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc != 0


def test_tab_spawner_is_none_on_headless_non_wsl_linux(monkeypatch):
    # Non-WSL Linux with no display has no tabs (Phase 3 desktop needs one),
    # so reopen degrades to detached-tmux rather than erroring.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.os, "environ", {})  # no display
    assert cli._tab_spawner(cfg.Config()) is None


def test_tab_spawner_selects_a_linux_terminal_on_a_desktop(monkeypatch):
    # Non-WSL Linux desktop with a display + an installed terminal.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.os, "environ", {"DISPLAY": ":0"})
    monkeypatch.setattr(cli.tab_spawn_linux.shutil, "which",
                        lambda b: "/usr/bin/kitty" if b == "kitty" else None)
    spawner = cli._tab_spawner(cfg.Config(overrides={"terminal": "kitty"}))
    assert isinstance(spawner, cli.tab_spawn_linux.LinuxTerminalSpawner)
    assert spawner.kind == "kitty"


def test_tab_spawner_selects_windows_terminal_under_wsl(monkeypatch):
    # WSL is checked before the Linux desktop path: wt.exe wins when present.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows.shutil, "which",
                        lambda b: "/mnt/c/wt.exe" if b == "wt.exe" else None)
    spawner = cli._tab_spawner(cfg.Config())
    assert isinstance(spawner, cli.tab_spawn_windows.WindowsTerminalSpawner)


def test_tab_spawner_is_none_on_other_platforms(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    assert cli._tab_spawner(cfg.Config()) is None


def test_tab_spawner_selects_a_macos_spawner_when_app_present(monkeypatch):
    # On macOS, _tab_spawner returns the chosen spawner when its app is
    # installed (available() true), or None when it isn't.
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.tab_spawn.subprocess, "run",
                        lambda cmd, **kw: type("R", (), {"returncode": 0})())
    spawner = cli._tab_spawner(cfg.Config(overrides={"terminal": "iterm"}))
    assert isinstance(spawner, cli.tab_spawn.ITerm2Spawner)
    monkeypatch.setattr(cli.tab_spawn.subprocess, "run",
                        lambda cmd, **kw: type("R", (), {"returncode": 1})())
    assert cli._tab_spawner(cfg.Config()) is None


def test_doctor_reports_install_health(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "boot-identity adapter" in out
    assert "tmux" in out
    assert "state dir" in out
    assert "crr-revive.timer" in out  # names the watchdog unit to install


def test_config_effective_lists_every_key_with_origin(capsys):
    rc = cli.main(["config", "--effective"])
    out = capsys.readouterr().out
    assert rc == 0
    for key in cfg.DEFAULTS:
        assert key in out
    assert "(default)" in out


def _live_entry(pid, boot_id):
    return {
        "v": 1,
        "pid": pid,
        "boot_id": boot_id,
        "cwd": "/home/u/project",
        "host": "tmux",
        "shell": "zsh",
        "claude": {
            "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "sid_source": "injected",
            "started": "2026-07-23T00:00:00Z",
        },
        "last_cmd": "claude",
        "tmux_session": None,
        "revive_strikes": 0,
        "updated": "2026-07-23T00:00:00Z",
    }


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter (Linux or macOS)")
def test_status_json_reports_live_process(tmp_path, monkeypatch, capsys):
    boot_id = boot_identity.detect().current()
    store = JournalStore(tmp_path)
    store.write(_live_entry(pid=os.getpid(), boot_id=boot_id))
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    rc = cli.main(["status", "--json"])
    out = capsys.readouterr().out
    assert rc == 0

    payload = json.loads(out)
    contracts.validate_sessions_payload(payload)  # emitted output honors the contract
    (card,) = payload["sessions"]
    assert card["pid"] == os.getpid()
    # This process is alive; with or without a tty it must be live or ghost.
    assert card["state"] in ("live", "ghost")


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter (Linux or macOS)")
def test_status_json_marks_rebooted_session_crashed(tmp_path, monkeypatch, capsys):
    # An entry from a different boot must classify crashed even though its
    # pid (ours) is alive — the recycled-pid guard, end to end.
    store = JournalStore(tmp_path)
    store.write(_live_entry(pid=os.getpid(), boot_id="00000000-0000-4000-8000-000000000000"))
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    cli.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["state"] == "crashed"


def _human_card(model):
    return {
        "pid": 42, "sid8": "8a1b2c3d", "state": "live", "cwd": "/home/u/proj",
        "model": model, "duplicate_group": None,
    }


def test_status_human_shows_model_when_known(capsys):
    cli._print_status_human({"sessions": [_human_card("claude-opus-5")]})
    assert "claude-opus-5" in capsys.readouterr().out


def test_status_human_omits_model_when_unknown(capsys):
    # No model read yet -> the line is the plain terse form, no trailing gap.
    cli._print_status_human({"sessions": [_human_card("")]})
    assert capsys.readouterr().out == "#42 · 8a1b2c3d [live] /home/u/proj\n"


# --- shim-facing commands: register / last-cmd / deregister --------------

def _seed(store, pid, cwd="/home/u/p", last_cmd=""):
    store.write(new_entry(
        pid=pid, cwd=cwd, host="tmux", shell="zsh",
        boot_id="b8f3c0de-0000-4000-8000-000000000000",
        now="2026-07-23T00:00:00Z", last_cmd=last_cmd,
    ))


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="register needs the boot adapter (Linux or macOS)")
def test_register_creates_claude_less_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["register", "--pid", "4242", "--cwd", "/home/u/proj",
                   "--shell", "zsh", "--host", "tmux"])
    assert rc == 0
    entry = JournalStore(tmp_path).read(4242)
    assert entry["claude"] is None
    assert entry["cwd"] == "/home/u/proj"
    assert entry["boot_id"] == boot_identity.detect().current()


def _claude_field(sid="8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"):
    return {"session_id": sid, "sid_source": "injected", "started": "2026-07-24T00:00:00Z"}


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="register needs the boot adapter (Linux or macOS)")
def test_register_after_reboot_archives_old_claude_session(tmp_path, monkeypatch):
    # A stale entry from before a reboot (different boot_id) carries revival
    # data. Register must preserve it in the archive, not clobber it.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    sid = "aaaaaaaa-1111-4111-8111-111111111111"
    store.write(new_entry(
        pid=1000, cwd="/old", host="tmux", shell="zsh",
        boot_id="pre-reboot-boot", now="2026-07-24T00:00:00Z", claude=_claude_field(sid),
    ))
    rc = cli.main(["register", "--pid", "1000", "--cwd", "/new", "--shell", "bash", "--host", "tab"])
    assert rc == 0
    # New active entry is fresh + claude-less; the old session is archived.
    assert store.read(1000)["claude"] is None
    assert store.read(1000)["cwd"] == "/new"
    rec = archive.read(sid)
    assert rec["reason"] == "superseded-on-register"
    assert rec["entry"]["claude"]["session_id"] == sid


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="register needs the boot adapter (Linux or macOS)")
def test_register_same_boot_preserves_claude_in_place(tmp_path, monkeypatch):
    # Same boot => can't tell an rc re-source from pid reuse. Preserve the
    # claude field (never wipe a possibly-live session, never risk a
    # duplicate revival); do NOT archive.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    boot = boot_identity.detect().current()
    sid = "bbbbbbbb-2222-4222-8222-222222222222"
    store.write(new_entry(
        pid=2000, cwd="/p", host="tmux", shell="zsh",
        boot_id=boot, now="2026-07-24T00:00:00Z", claude=_claude_field(sid),
        tmux_session="crr-bbbbbbbb", revive_strikes=1,
    ))
    rc = cli.main(["register", "--pid", "2000", "--cwd", "/p", "--shell", "zsh", "--host", "tmux"])
    assert rc == 0
    entry = store.read(2000)
    assert entry["claude"]["session_id"] == sid  # preserved, not wiped
    assert entry["tmux_session"] == "crr-bbbbbbbb"
    assert entry["revive_strikes"] == 1
    assert archive.scan().records == []  # nothing archived on same boot


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="register needs the boot adapter (Linux or macOS)")
def test_register_over_claude_less_entry_does_not_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    store.write(new_entry(
        pid=3000, cwd="/p", host="tmux", shell="zsh",
        boot_id="whatever", now="2026-07-24T00:00:00Z", claude=None,
    ))
    assert cli.main(["register", "--pid", "3000", "--cwd", "/p2", "--shell", "zsh", "--host", "tmux"]) == 0
    assert archive.scan().records == []  # no revival data => nothing to preserve


def test_last_cmd_updates_existing_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242, cwd="/old", last_cmd="")
    rc = cli.main(["last-cmd", "--pid", "4242", "--cmd", "claude --resume", "--cwd", "/new"])
    assert rc == 0
    entry = store.read(4242)
    assert entry["last_cmd"] == "claude --resume"
    assert entry["cwd"] == "/new"


def test_last_cmd_on_missing_pid_is_quiet_noop(tmp_path, monkeypatch):
    # Hot-path hook: never disrupt the prompt if the entry is gone.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["last-cmd", "--pid", "999", "--cmd", "x"])
    assert rc == 0
    assert not JournalStore(tmp_path).tabs_dir.joinpath("999.json").exists()


def test_deregister_removes_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    assert cli.main(["deregister", "--pid", "4242"]) == 0
    assert not store.tabs_dir.joinpath("4242.json").exists()
    assert cli.main(["deregister", "--pid", "4242"]) == 0  # second call: no error


# --- claude() wrapper support: claude-launch / claude-exit ---------------

def test_claude_launch_injects_sid_and_journals_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    rc = cli.main(["claude-launch", "--pid", "4242"])
    sid = capsys.readouterr().out.strip()
    assert rc == 0
    assert len(sid) == 36 and sid.count("-") == 4  # a uuid was printed
    claude = store.read(4242)["claude"]
    assert claude["session_id"] == sid
    assert claude["sid_source"] == "injected"  # wrapper-generated => certain
    assert claude["started"]


def test_claude_launch_archives_a_superseded_session(tmp_path, monkeypatch, capsys):
    # Same-boot pid reuse: entry already carries a (now-dead) claude session
    # X; launching Y must preserve X in the archive, not silently drop it.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    x = "aaaaaaaa-1111-4111-8111-111111111111"
    store.write(new_entry(
        pid=4242, cwd="/p", host="tmux", shell="zsh",
        boot_id="b", now="2026-07-24T00:00:00Z", claude=_claude_field(x),
    ))
    cli.main(["claude-launch", "--pid", "4242"])
    y = capsys.readouterr().out.strip()

    assert y != x
    assert store.read(4242)["claude"]["session_id"] == y  # new session active
    rec = archive.read(x)  # old session preserved
    assert rec["reason"] == "superseded-on-launch"


def test_claude_launch_honors_explicit_session_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    given = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    cli.main(["claude-launch", "--pid", "4242", "--session-id", given])
    assert capsys.readouterr().out.strip() == given
    assert store.read(4242)["claude"]["session_id"] == given


def test_claude_launch_rejects_non_uuid_explicit_session_id(tmp_path, monkeypatch, capsys):
    # A user-typed `claude -r '../tabs/99'` (or similar junk) forwarded here
    # as --session-id must never be journaled (audit 2026-07-29: path
    # traversal via ArchiveStore.path_for). The wrapper's contract of always
    # printing a sid for claude to use is kept; just nothing is journaled.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    rc = cli.main(["claude-launch", "--pid", "4242", "--session-id", "../tabs/99"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "../tabs/99"
    assert store.read(4242)["claude"] is None  # never journaled


def test_claude_launch_missing_entry_still_prints_a_sid(tmp_path, monkeypatch, capsys):
    # Shell wasn't registered: best-effort, claude must still get a sid.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["claude-launch", "--pid", "999"])
    sid = capsys.readouterr().out.strip()
    assert rc == 0 and len(sid) == 36


def test_claude_exit_clears_claude_field(tmp_path, monkeypatch, capsys):
    # Clean exit clears claude -> a live shell with no active session. A
    # crash would skip this, leaving claude set for the reviver.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    cli.main(["claude-launch", "--pid", "4242"])
    capsys.readouterr()
    assert store.read(4242)["claude"] is not None
    assert cli.main(["claude-exit", "--pid", "4242"]) == 0
    assert store.read(4242)["claude"] is None


def test_claude_exit_missing_entry_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    assert cli.main(["claude-exit", "--pid", "999"]) == 0


# --- claude() wrapper support: claude-resume (guessed / verified sids) ----

def _write_transcript_file(home, cwd, sid, mtime=None):
    d = home / ".claude" / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_claude_resume_verifies_an_explicit_sid_with_a_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    sid = "eeeeeeee-5555-4555-8555-555555555555"
    _write_transcript_file(tmp_path / "home", "/home/u/proj", sid)
    rc = cli.main(["claude-resume", "--pid", "4242", "--cwd", "/home/u/proj",
                   "--session-id", sid])
    assert rc == 0
    claude = store.read(4242)["claude"]
    assert claude["session_id"] == sid
    assert claude["sid_source"] == "verified"  # its transcript exists


def test_claude_resume_guesses_newest_transcript_without_explicit_sid(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    older = "11111111-aaaa-4aaa-8aaa-111111111111"
    newest = "22222222-bbbb-4bbb-8bbb-222222222222"
    _write_transcript_file(tmp_path / "home", "/home/u/proj", older, mtime=1000)
    _write_transcript_file(tmp_path / "home", "/home/u/proj", newest, mtime=5000)
    rc = cli.main(["claude-resume", "--pid", "4242", "--cwd", "/home/u/proj"])
    assert rc == 0
    claude = store.read(4242)["claude"]
    assert claude["session_id"] == newest
    assert claude["sid_source"] == "guessed"


def test_claude_resume_leaves_untracked_when_no_sid_and_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    rc = cli.main(["claude-resume", "--pid", "4242", "--cwd", "/home/u/proj"])
    assert rc == 0
    assert store.read(4242)["claude"] is None  # nothing to guess -> untracked


_G1_SID = "11112222-3333-4444-5555-666677778888"


def test_verify_guessed_sids_upgrades_when_transcript_is_active(tmp_path, monkeypatch):
    # The revive-sweep helper upgrades a guessed sid to verified once its
    # transcript shows activity after the session started.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    entry = new_entry(
        pid=7, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="b", now="2026-07-25T12:00:00+00:00",
        claude={"session_id": _G1_SID, "sid_source": "guessed",
                "started": "2026-07-25T12:00:00+00:00"},
    )
    store.write(entry)
    started = datetime.fromisoformat("2026-07-25T12:00:00+00:00").timestamp()
    _write_transcript_file(tmp_path / "home", "/home/u/proj", _G1_SID, mtime=started + 60)

    cli._verify_guessed_sids(store, "2026-07-25T12:05:00+00:00")
    assert store.read(7)["claude"]["sid_source"] == "verified"


def test_verify_guessed_sids_leaves_idle_guess_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    entry = new_entry(
        pid=7, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="b", now="2026-07-25T12:00:00+00:00",
        claude={"session_id": _G1_SID, "sid_source": "guessed",
                "started": "2026-07-25T12:00:00+00:00"},
    )
    store.write(entry)
    started = datetime.fromisoformat("2026-07-25T12:00:00+00:00").timestamp()
    _write_transcript_file(tmp_path / "home", "/home/u/proj", _G1_SID, mtime=started - 30)  # pre-launch
    cli._verify_guessed_sids(store, "2026-07-25T12:05:00+00:00")
    assert store.read(7)["claude"]["sid_source"] == "guessed"  # unconfirmed stays guessed


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter")
def test_status_upgrades_guessed_sid_when_transcript_confirms(tmp_path, monkeypatch, capsys):
    # [audit P3] "stays guessed until a watchdog pass" — status itself must
    # upgrade, so a dashboard without the watchdog still converges.
    sid = "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55"
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=7, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="b", now="2026-07-30T00:00:00+00:00",
        claude={"session_id": sid, "sid_source": "guessed",
                "started": "2026-07-30T00:00:00+00:00"},
    ))
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.transcript_source, "list_transcripts",
                         lambda cwd, home=None: [{"session_id": sid, "mtime": 1e12}])

    rc = cli.main(["status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["sid_source"] == "verified"
    assert store.read(7)["claude"]["sid_source"] == "verified"  # upgrade is durable, not just in-payload


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter")
def test_status_stays_lock_free_when_no_guess_is_upgradable(tmp_path, monkeypatch, capsys):
    # The whole point of the pre-scan: when nothing is upgradeable (no
    # guessed entries, or a guess the transcript doesn't yet confirm), the
    # poll path never takes the mutation lock.
    sid = "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55"
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=7, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="b", now="2026-07-30T00:00:00+00:00",
        claude={"session_id": sid, "sid_source": "guessed",
                "started": "2026-07-30T00:00:00+00:00"},
    ))
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.transcript_source, "list_transcripts",
                         lambda cwd, home=None: [{"session_id": sid, "mtime": 0}])  # pre-launch, unconfirmed

    def _boom(*a, **k):
        raise AssertionError("poll path must stay lock-free when nothing is upgradable")
    monkeypatch.setattr(cli, "mutation_lock", _boom)

    rc = cli.main(["status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["sid_source"] == "guessed"


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter")
def test_revive_verifies_guessed_sids_and_the_upgrade_survives_the_sweep(tmp_path, monkeypatch):
    # End-to-end through `crr revive`: the guessed->verified upgrade must
    # survive revive's own store.write of the same entry — pins the re-scan
    # ordering (without it, revive would write back the stale guessed dict).
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return set()  # nothing live -> the crashed entry is revived

        def new_detached_session(self, name, cwd, argv):
            pass

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)

    store = JournalStore(tmp_path / "state")
    store.write(new_entry(
        pid=7, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="an-old-boot-that-cannot-match",  # boot mismatch => crashed
        now="2026-07-25T12:00:00+00:00",
        claude={"session_id": _G1_SID, "sid_source": "guessed",
                "started": "2026-07-25T12:00:00+00:00"},
    ))
    started = datetime.fromisoformat("2026-07-25T12:00:00+00:00").timestamp()
    _write_transcript_file(tmp_path / "home", "/home/u/proj", _G1_SID, mtime=started + 60)

    assert cli.main(["revive"]) == 0
    entry = store.read(7)
    assert entry["claude"]["sid_source"] == "verified"  # upgrade survived revive's write
    assert entry["tmux_session"] == f"crr-{_G1_SID[:8]}"  # and it was actually revived


# --- revive: crashed claude session -> detached tmux (end to end) ---------

# --- session ops: remove / dismiss / reopen -----------------------------

def test_gc_removes_expired_archive_records_only(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    archive = ArchiveStore(tmp_path)
    old = _live_entry(pid=1, boot_id="b")
    old["claude"] = _claude_field("11111111-1111-4111-8111-111111111111")
    fresh = _live_entry(pid=2, boot_id="b")
    fresh["claude"] = _claude_field("22222222-2222-4222-8222-222222222222")
    archive.archive(old, "gave-up", "2026-01-01T00:00:00+00:00")     # long ago
    archive.archive(fresh, "dismissed", "2099-01-01T00:00:00+00:00")  # future => kept
    assert cli.main(["gc"]) == 0
    sids = {r["entry"]["claude"]["session_id"] for r in archive.scan().records}
    assert sids == {"22222222-2222-4222-8222-222222222222"}  # only the fresh one remains


def test_remove_delists_without_archiving(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    store.write(new_entry(
        pid=42, cwd="/p", host="tmux", shell="zsh", boot_id="b",
        now="2026-07-24T00:00:00Z", claude=_claude_field(),
    ))
    assert cli.main(["remove", "--pid", "42"]) == 0
    assert not store.tabs_dir.joinpath("42.json").exists()
    assert archive.scan().records == []  # pure delist: nothing archived
    assert cli.main(["remove", "--pid", "42"]) == 0  # idempotent


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="dismiss classifies (needs Linux or macOS boot adapter)")
def test_dismiss_archives_crashed_claude_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    sid = "cccccccc-3333-4333-8333-333333333333"
    store.write(new_entry(  # different boot => crashed
        pid=42, cwd="/p", host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid),
    ))
    assert cli.main(["dismiss", "--pid", "42"]) == 0
    assert not store.tabs_dir.joinpath("42.json").exists()
    assert archive.read(sid)["reason"] == "dismissed"


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="dismiss classifies (needs Linux or macOS boot adapter)")
def test_dismiss_refuses_a_live_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    boot = boot_identity.detect().current()
    store.write(new_entry(  # same boot + our live pid => not crashed
        pid=os.getpid(), cwd="/p", host="tmux", shell="zsh", boot_id=boot,
        now="2026-07-24T00:00:00Z", claude=_claude_field(),
    ))
    rc = cli.main(["dismiss", "--pid", str(os.getpid())])
    assert rc != 0  # refuse to dismiss a live session
    assert store.tabs_dir.joinpath(f"{os.getpid()}.json").exists()  # untouched


@pytest.mark.skipif(
    platform.system() not in ("Linux", "Darwin") or shutil.which("tmux") is None,
    reason="reopen needs Linux boot adapter + tmux",
)
def test_reopen_revives_one_crashed_session(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text("#!/usr/bin/env bash\nexec sleep 300\n", encoding="utf-8")
    (bindir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    store = JournalStore(tmp_path)
    sid = "dddddddd-4444-4444-8444-444444444444"
    store.write(new_entry(  # crashed (old boot) + claude
        pid=42, cwd=str(tmp_path), host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid),
    ))
    try:
        assert cli.main(["reopen", "--pid", "42"]) == 0
        assert store.read(42)["tmux_session"] == f"crr-{sid[:8]}"
        sessions = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        ).stdout
        assert f"crr-{sid[:8]}" in sessions
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="reopen classifies (needs Linux or macOS boot adapter)")
def test_reopen_refuses_claude_less_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=42, cwd="/p", host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=None,
    ))
    assert cli.main(["reopen", "--pid", "42"]) != 0  # nothing to resume


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="boot adapter")
def test_kick_refuses_a_crashed_session(tmp_path, monkeypatch, capsys):
    # A crashed entry is refused BEFORE any signalling (classifier gate),
    # so this exercises the CLI wiring without touching real processes.
    store = JournalStore(tmp_path)
    store.write(_live_entry(pid=os.getpid(),
                            boot_id="00000000-0000-4000-8000-000000000000"))  # foreign boot -> crashed
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["kick", str(os.getpid())])
    out = capsys.readouterr().out
    assert rc == 1
    assert "crashed" in out


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="boot adapter")
def test_close_reports_no_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["close", "424242"])
    assert rc == 1
    assert "no session" in capsys.readouterr().out


@pytest.mark.skipif(
    shutil.which("tmux") is None or platform.system() not in ("Linux", "Darwin"),
    reason="detmux needs tmux + Linux/macOS boot adapter",
)
def test_detmux_reports_no_session(tmp_path, monkeypatch, capsys):
    # tmux is present (RealTmux.available() gate passes), so the store
    # lookup is what fails — exercising the CLI wiring (parser ->
    # mutation_lock -> ops.detmux) without touching real tmux state.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["detmux", "424242"])
    assert rc == 1
    assert "no session" in capsys.readouterr().err


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="detmux classifies (needs Linux or macOS boot adapter)")
def test_detmux_attaches_a_session_via_cli(tmp_path, monkeypatch, capsys):
    # End-to-end through `crr detmux` with a fake tmux + fake (always-
    # available) tab spawner standing in for the real adapters — pins the
    # CLI wiring (parser -> mutation_lock -> ops.detmux with the journaled
    # store) reaching a "live" tmux session and clearing its bookkeeping,
    # without spawning any real tmux server (real-tmux semantics are
    # already covered by Task 1's ops-level tests).
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    name = "crr-detmuxtest"

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return {name}

    class _FakeTab:
        def available(self):
            return True

        def open_tab(self, argv):
            pass

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())

    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=42, cwd=str(tmp_path), host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(),
    ))
    entry = store.read(42)
    entry["tmux_session"] = name
    store.write(entry)

    rc = cli.main(["detmux", "42"])
    assert rc == 0
    with pytest.raises(KeyError):
        store.read(42)
    out = capsys.readouterr().out
    assert "de-tmuxed" in out
    assert "crr no longer manages it" in out


@pytest.mark.skipif(
    shutil.which("tmux") is None or platform.system() not in ("Linux", "Darwin"),
    reason="untmux needs tmux + Linux/macOS boot adapter",
)
def test_untmux_reports_no_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["untmux", "424242"])
    assert rc == 1
    assert "no session" in capsys.readouterr().err


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="untmux classifies (needs Linux or macOS boot adapter)")
def test_untmux_kills_and_relaunches_via_cli(tmp_path, monkeypatch, capsys):
    # Mirrors test_detmux_attaches_a_session_via_cli: fake tmux + fake
    # (always-available) tab spawner stand in for the real adapters, so this
    # pins the CLI wiring (parser -> mutation_lock -> ops.untmux with the
    # journaled store) without touching real tmux state.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    name = "crr-untmuxtest"

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return {name}

        def kill_session(self, session_name):
            pass

    class _FakeTab:
        def available(self):
            return True

        def open_tab(self, argv, cwd=None):
            pass

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())

    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=42, cwd=str(tmp_path), host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(),
    ))
    entry = store.read(42)
    entry["tmux_session"] = name
    store.write(entry)

    rc = cli.main(["untmux", "42"])
    assert rc == 0
    with pytest.raises(KeyError):
        store.read(42)
    out = capsys.readouterr().out
    assert "un-tmuxed" in out
    assert "crr no longer manages it" in out


class _FakeBoot:
    """Stands in for boot_identity.detect() so `rescued` tests aren't
    gated to a real Linux/macOS boot adapter (mirrors the fake-tmux
    technique used by test_detmux_attaches_a_session_via_cli)."""

    def current(self):
        return "current-boot"


class _FakeTmuxRescued:
    def __init__(self, *a, **k):
        pass

    def available(self):
        return True

    def list_sessions(self):
        return {"crr-8a1b2c3d"}


def test_rescued_lists_prior_boot_parked_sessions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)

    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    store = JournalStore(tmp_path)
    store.write(new_entry(  # prior-boot entry parked in a live tmux session
        pid=42, cwd=str(tmp_path), host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid), tmux_session="crr-8a1b2c3d",
    ))

    rc = cli.main(["rescued"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"#42 · 8a1b2c3d {tmp_path} → crr-8a1b2c3d" in out
    assert "attach: tmux attach -t <name> · dashboard: Reopen/Untrack" in out


def test_rescued_reports_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)

    rc = cli.main(["rescued"])
    assert rc == 0
    assert "no rescued sessions" in capsys.readouterr().out


def _rescue_check_setup(monkeypatch, tmp_path, found):
    """Common wiring for `crr rescue-check` tests: fake boot + fake tmux
    (SAFETY: never the real adapters — this machine runs production crr
    with live sessions) and a monkeypatched rescue.rescued_sessions that
    ignores its (journal/boot/live-tmux) inputs and returns `found`
    directly, so tests don't need real journal fixtures."""
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)
    monkeypatch.setattr(cli.rescue, "rescued_sessions", lambda *a, **k: found)


def test_rescue_check_silent_when_marker_exists(tmp_path, monkeypatch, capsys):
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    cli.rescue.mark_prompted(tmp_path, "current-boot")  # already prompted this boot
    calls = []
    monkeypatch.setattr(cli.ops, "detmux", lambda *a, **k: calls.append(a))

    rc = cli.main(["rescue-check"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "" and err == ""
    assert calls == []


def test_rescue_check_silent_when_not_a_tty(tmp_path, monkeypatch, capsys):
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    rc = cli.main(["rescue-check"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "" and err == ""
    # a later interactive shell must still be offered -> marker NOT written
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is False


def test_rescue_check_headless_prints_notice_once(tmp_path, monkeypatch, capsys):
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config: None)

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 conversation(s) rescued from the last reboot" in out
    assert "'crr rescued' lists them" in out
    assert "tmux attach -t <name>" in out
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True

    rc2 = cli.main(["rescue-check"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert out2 == ""  # once-per-boot: second call is silent


def test_rescue_check_yes_opens_tabs_and_marks(tmp_path, monkeypatch, capsys):
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    calls = []

    def fake_detmux(store, archive, tmux_spawner, boot, probe, pid, now, tab_spawner=None):
        calls.append(pid)
        return SimpleNamespace(ok=True, message=f"crr: #{pid} de-tmuxed")

    monkeypatch.setattr(cli.ops, "detmux", fake_detmux)

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [42, 43]
    assert "#42 de-tmuxed" in out and "#43 de-tmuxed" in out
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_enter_defaults_to_yes(tmp_path, monkeypatch, capsys):
    # The decided-and-recorded half of the yes/no split: a typed EMPTY
    # line (just pressing Enter) is yes -- distinct from a TIMEOUT, which
    # is always "not now" (see test_rescue_check_timeout_declines). An
    # implementation that only accepts a literal "y" would pass every
    # other test here but fail this one.
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "\n")  # Enter, no text

    calls = []

    def fake_detmux(store, archive, tmux_spawner, boot, probe, pid, now, tab_spawner=None):
        calls.append(pid)
        return SimpleNamespace(ok=True, message=f"crr: #{pid} de-tmuxed")

    monkeypatch.setattr(cli.ops, "detmux", fake_detmux)

    rc = cli.main(["rescue-check"])
    assert rc == 0
    assert calls == [42]
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_eof_declines(tmp_path, monkeypatch, capsys):
    # stdin closed mid-read (readline returns "") is a decline, same as a
    # timeout -- not an accident of "" also meaning Enter, since a real
    # EOF never reaches the strip()/lower() step that "\n" does.
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "")  # EOF

    calls = []
    monkeypatch.setattr(cli.ops, "detmux", lambda *a, **k: calls.append(1))

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not now" in out
    assert calls == []
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_timeout_declines(tmp_path, monkeypatch, capsys):
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: ([], [], []))

    calls = []
    monkeypatch.setattr(cli.ops, "detmux", lambda *a, **k: calls.append(1))

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not now" in out
    assert "'crr rescued' lists them" in out
    assert calls == []
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_keyboard_interrupt_declines(tmp_path, monkeypatch, capsys):
    # Ctrl-C while waiting at the [Y/n] prompt must behave like a timeout
    # (decline + marker written), not propagate — a shim hook that lets a
    # KeyboardInterrupt escape both breaks the "never break the shell"
    # guarantee AND, because it would unwind past mark_prompted, breaks
    # the once-per-boot invariant (next shell prompts again).
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())

    def _raise_keyboard_interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.select, "select", _raise_keyboard_interrupt)

    calls = []
    monkeypatch.setattr(cli.ops, "detmux", lambda *a, **k: calls.append(1))

    rc = cli.main(["rescue-check"])  # must not raise
    out = capsys.readouterr().out
    assert rc == 0
    assert "not now" in out
    assert "'crr rescued' lists them" in out
    assert calls == []
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_repair_check_prints_relaunch_kind_and_sid(tmp_path, monkeypatch, capsys):
    from crr.core.flags import FlagStore
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    FlagStore(tmp_path).arm_relaunch(4242, "sid-xyz")
    rc = cli.main(["repair-check", "--pid", "4242"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "relaunch sid-xyz"


def test_repair_check_prints_close_kind(tmp_path, monkeypatch, capsys):
    from crr.core.flags import FlagStore
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    FlagStore(tmp_path).arm_close(4242)
    rc = cli.main(["repair-check", "--pid", "4242"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "close"


def test_repair_check_absent_prints_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["repair-check", "--pid", "4242"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_repair_check_clear_unlinks_the_flag(tmp_path, monkeypatch, capsys):
    from crr.core.flags import FlagStore
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    flags = FlagStore(tmp_path)
    flags.arm_close(4242)
    rc = cli.main(["repair-check", "--pid", "4242", "--clear"])
    assert rc == 0
    assert flags.read(4242) is None


@pytest.mark.skipif(
    platform.system() not in ("Linux", "Darwin") or shutil.which("tmux") is None,
    reason="needs Linux boot adapter + tmux",
)
def test_revive_spawns_tmux_for_crashed_claude_session(tmp_path, monkeypatch):
    # Fake claude that stays alive, so the revived session persists.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text("#!/usr/bin/env bash\nexec sleep 300\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    store = JournalStore(tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    # A different boot_id => classifier crashed; claude set => resumable.
    store.write(new_entry(
        pid=4242, cwd=str(tmp_path), host="tmux", shell="zsh",
        boot_id="00000000-0000-4000-8000-000000000000", now="2026-07-24T00:00:00Z",
        claude={"session_id": sid, "sid_source": "injected", "started": "2026-07-24T00:00:00Z"},
    ))
    try:
        rc = cli.main(["revive"])
        assert rc == 0
        entry = store.read(4242)
        assert entry["tmux_session"] == f"crr-{sid[:8]}"
        assert entry["revive_strikes"] == 1
        sessions = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        ).stdout
        assert f"crr-{sid[:8]}" in sessions
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
