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

from conftest import set_home  # tests/ is on sys.path (no __init__.py)
from crr import cli
from crr.adapters import boot_identity, process_probe, session_state, state_dir
from crr.core import config as cfg
from crr.core import contracts
from crr.core import dashboard_auth
from crr.core import discovery
from crr.core.archive import ArchiveStore
from crr.core.journal import JournalStore, new_entry
from crr.core.ports import ResumeProcess


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
    # F11: params carries the generating caps/lookback/timeout even when the
    # source degraded — the lineage is about what was ASKED, not just answered.
    assert set(payload["params"]) == {"lookback_boots", "event_cap", "line_cap", "timeout_seconds"}


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
    assert set(payload["params"]) == {"lookback", "event_cap", "timeout_seconds"}


def test_diagnostics_params_named_per_source_semantics():
    # F11: params must record only the config keys the selected source
    # actually reads — recording e.g. macOS's timeout key for journald
    # would be a lineage lie, not a lineage.
    config = cfg.Config()
    assert cli._diagnostics_params(cli.diag_source, config) == {
        "lookback_boots": config.get("diagnose_lookback_boots"),
        "event_cap": config.get("diagnose_event_cap"),
        "line_cap": config.get("diagnose_line_cap"),
        "timeout_seconds": config.get("interop_timeout_seconds"),
    }
    assert cli._diagnostics_params(cli.diagnostics_macos, config) == {
        "lookback": config.get("diagnose_macos_lookback"),
        "event_cap": config.get("diagnose_event_cap"),
        "timeout_seconds": config.get("diagnose_macos_timeout_seconds"),
    }
    assert cli._diagnostics_params(cli.diagnostics_windows, config) == {
        "event_cap": config.get("diagnose_event_cap"),
        "timeout_seconds": config.get("interop_timeout_seconds"),
    }


def test_diagnostics_params_rejects_an_unrecognized_source():
    # Finding 4 (re-audit): the old code fell through to journald's params
    # for ANY source that wasn't macOS/Windows — so a future adapter would
    # silently inherit journald's lineage claim instead of failing loudly.
    config = cfg.Config()

    class _FutureSource:
        SOURCE_NAME = "future-source"

    with pytest.raises(ValueError, match="future-source"):
        cli._diagnostics_params(_FutureSource(), config)


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


def _diag_payload(boots):
    return {
        "contract": contracts.DIAGNOSTICS_CONTRACT_VERSION,
        "source": "journald",
        "summary": ["looks clean — no shutdown/OOM/watchdog signature found."],
        "boots": boots,
        "prev_boot_errors": [],
        "host_events": [],
        "degraded": [],
        "params": {"lookback_boots": 1, "event_cap": 50, "line_cap": 200, "timeout_seconds": 5},
    }


def test_diagnose_human_prints_source_and_boot_line(monkeypatch, capsys):
    # F12: neither consumer rendered the payload's source/boots lineage.
    boots = [{"index": -1, "boot_id": "b1", "start": "2026-07-23T00:00:00+00:00", "stop": ""}]
    monkeypatch.setattr(cli, "gather_diagnostics", lambda config: _diag_payload(boots))
    rc = cli.main(["diagnose"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == "source: journald"
    assert lines[1].startswith("boot: b1")


def test_diagnose_human_omits_boot_line_when_no_boots(monkeypatch, capsys):
    monkeypatch.setattr(cli, "gather_diagnostics", lambda config: _diag_payload([]))
    rc = cli.main(["diagnose"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == "source: journald"
    assert "boot:" not in out


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


def test_systemd_print_emits_all_units_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    rc = cli.main(["systemd"])  # default: print, no --install
    out = capsys.readouterr().out
    assert rc == 0
    assert "crr-revive.service" in out and "crr-revive.timer" in out
    assert "ExecStart=/opt/crr/bin/crr revive" in out
    # crr-web.service and crr-awake.service (own unit, own lifetime — see
    # systemd.awake_service_unit's docstring) round out the four units.
    assert cli.systemd.WEB_SERVICE_NAME in out
    assert cli.systemd.AWAKE_SERVICE_NAME in out
    assert "ExecStart=/opt/crr/bin/crr awake" in out
    # XDG_STATE_HOME baked as the parent of the resolved state dir.
    assert f"Environment=XDG_STATE_HOME={tmp_path / 'state'}" in out
    # [Task 7] print mode must still list linger (enable_commands(), not the
    # critical-only split) — pinned at the CLI boundary, not just algebraically
    # in test_systemd.py, so a future cli.py swap to critical_enable_commands()
    # here would be caught.
    assert "loginctl enable-linger" in out
    # Print mode must not touch the user's systemd dir.
    assert not (tmp_path / ".config" / "systemd").exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="asserts on POSIX path literals baked into a unit's PATH=; under "
           "ntpath they acquire drive letters, so what the assertion "
           "measures is path semantics rather than crr. The rest of the "
           "systemd/launchd tests are path-agnostic and do run here",
)
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


def test_systemd_print_warns_when_interop_is_unregistered_despite_wt_on_path(
    tmp_path, monkeypatch, capsys
):
    # [live bug, 2026-08-09] On DrvFs every file looks executable, so a PATH
    # check alone reports wt.exe/wsl.exe healthy while the kernel cannot exec
    # them (missing WSLInterop binfmt handler → ENOEXEC). Warning only on
    # "not found" would tell the operator tab spawning is fine when it isn't.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.systemd.shutil, "which", lambda name: f"/mnt/c/{name}")
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: False)
    rc = cli.main(["systemd"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "not found on PATH" not in err  # they ARE on PATH — say the true thing
    assert "WSLInterop" in err
    assert "tab" in err.lower()
    assert "revived sessions will fail on exec" not in err


def test_systemd_print_is_silent_about_interop_when_it_is_registered(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.systemd.shutil, "which", lambda name: f"/mnt/c/{name}")
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: True)
    assert cli.main(["systemd"]) == 0
    assert "WSLInterop" not in capsys.readouterr().err


def test_systemd_print_does_not_check_interop_off_wsl(tmp_path, monkeypatch, capsys):
    # Native Linux has no interop to be missing; never consult it, never warn.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.systemd.shutil, "which", lambda name: f"/usr/bin/{name}")

    def boom():
        raise AssertionError("interop must not be consulted off WSL")

    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", boom)
    assert cli.main(["systemd"]) == 0
    assert "WSLInterop" not in capsys.readouterr().err


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


def test_launchd_print_emits_all_agents_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    rc = cli.main(["launchd"])  # default: print, no --install
    out = capsys.readouterr().out
    assert rc == 0
    assert "com.claude-remote-rescue.revive.plist" in out
    assert "com.claude-remote-rescue.web.plist" in out
    # crr-awake (own agent, own lifetime — see launchd.awake_agent_plist's
    # docstring) rounds out the three agents.
    assert "com.claude-remote-rescue.awake.plist" in out
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


# --- F6: --port default=None, resolved from config's dashboard_port -------

def test_systemd_print_default_port_comes_from_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    rc = cli.main(["systemd"])  # no --port -> config dashboard_port default (8377)
    out = capsys.readouterr().out
    assert rc == 0
    assert "web --port 8377" in out


def test_systemd_print_default_port_honors_config_toml_override(tmp_path, monkeypatch, capsys):
    sd = tmp_path / "state" / "crr"
    sd.mkdir(parents=True)
    (sd / "config.toml").write_text("dashboard_port = 9001\n", encoding="utf-8")
    monkeypatch.setattr(state_dir, "state_dir", lambda: sd)
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    rc = cli.main(["systemd"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "web --port 9001" in out


def test_systemd_print_explicit_port_still_overrides_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    rc = cli.main(["systemd", "--port", "9999"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "web --port 9999" in out


def test_launchd_print_default_port_comes_from_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/opt/crr/bin/crr")
    rc = cli.main(["launchd"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "8377" in out


def test_schtasks_print_default_port_comes_from_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state" / "crr")
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/usr/bin/crr")
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("print must not run schtasks")))
    rc = cli.main(["schtasks"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "web --port 8377" in out


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"),
                     reason="needs the boot-identity adapter (Linux or macOS)")
def test_web_default_port_comes_from_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    class _FakeServer:
        def __init__(self, addr, handler):
            _FakeServer.addr = addr

        def serve_forever(self):
            raise KeyboardInterrupt()

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "ThreadingHTTPServer", _FakeServer)
    rc = cli.main(["web"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _FakeServer.addr == ("127.0.0.1", 8377)
    assert "http://127.0.0.1:8377/" in out


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"),
                     reason="needs the boot-identity adapter (Linux or macOS)")
def test_web_explicit_port_still_overrides_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    class _FakeServer:
        def __init__(self, addr, handler):
            _FakeServer.addr = addr

        def serve_forever(self):
            raise KeyboardInterrupt()

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "ThreadingHTTPServer", _FakeServer)
    rc = cli.main(["web", "--port", "9123"])
    assert rc == 0
    assert _FakeServer.addr == ("127.0.0.1", 9123)


def test_doctor_uses_configured_interop_timeout_for_systemctl_check(tmp_path, monkeypatch, capsys):
    # F5: doctor's systemctl call literal `timeout=5` duplicated
    # interop_timeout_seconds instead of reading it.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / "config.toml").write_text("interop_timeout_seconds = 42\n", encoding="utf-8")
    monkeypatch.setattr(cli.systemd, "unit_dir", lambda home: tmp_path)
    (tmp_path / cli.systemd.TIMER_NAME).write_text("", encoding="utf-8")
    (tmp_path / cli.systemd.WEB_SERVICE_NAME).write_text("", encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which",
                        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    captured = {}

    def fake_run(cmd, **kwargs):
        # Only the systemctl probe is under test; record its timeout. Other
        # callers (doctor's #61 deploy-drift probe) just need a well-formed
        # CompletedProcess stand-in — returncode included.
        if cmd and cmd[0].endswith("systemctl"):
            captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.main(["doctor"])
    assert rc == 0
    assert captured["timeout"] == 42


def test_doctor_reports_a_malformed_config_toml_exactly_once(tmp_path, monkeypatch, capsys):
    # A malformed config.toml must be reported once, not twice: doctor's
    # own [WARN] config.toml line is the single source of truth here, not
    # duplicated by a second, independent _load_config() stderr message.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / "config.toml").write_text('zombie_strikes = "not-an-int"\n', encoding="utf-8")
    rc = cli.main(["doctor"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert err == ""  # no duplicate "crr: ignoring bad config" on stderr
    assert out.count("zombie_strikes") == 1
    assert "[WARN] config.toml" in out


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


def test_systemd_install_linger_failure_is_a_warning_not_a_failure(tmp_path, monkeypatch, capsys):
    """[Task 7, live evidence 2026-07-31] on WSL2 `loginctl enable-linger`
    reliably exits 1 (benign dbus quirk) while daemon-reload/enable succeed
    and the services really do run. Only linger failing must NOT fail the
    install — it must warn on stderr and still report success."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        rc = 1 if cmd == cli.systemd.linger_command() else 0
        return subprocess.CompletedProcess(cmd, returncode=rc)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.main(["systemd", "--install", "--crr-bin", "/usr/bin/crr"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "installed watchdog" in out
    assert (
        "crr systemd: warning — could not enable linger (common on WSL2); "
        "services will stop at logout unless linger is enabled another way"
    ) in err
    # Exactly one line, and NOT the generic _run_commands-style "exited 1" —
    # the whole point of bypassing _run_commands for linger.
    assert "exited 1" not in err
    assert err.count("could not enable linger") == 1
    assert cli.systemd.linger_command() in calls


def test_systemd_install_enable_failure_still_fails_even_if_linger_would_succeed(
    tmp_path, monkeypatch, capsys
):
    """A real enable failure (not linger) must still hard-fail the install —
    the linger carve-out must not swallow other failures."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, **kwargs):
        rc = 1 if cmd[:2] == ["systemctl", "--user"] and "enable" in cmd else 0
        return subprocess.CompletedProcess(cmd, returncode=rc)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.main(["systemd", "--install", "--crr-bin", "/usr/bin/crr"])
    out, err = capsys.readouterr()
    assert rc != 0
    assert "installed watchdog" not in out
    assert "NOT running" in err
    # The linger carve-out must not mask/mention this failure.
    assert "could not enable linger" not in err


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


def test_detmux_is_an_alias_for_untrack(monkeypatch):
    """Terminology change: detmux -> untrack; 'detmux' stays a deprecated
    alias (mirrors restore->reopen)."""
    seen = {}

    def fake_untrack(args):
        seen["pid"] = args.pid
        return 0

    monkeypatch.setattr(cli, "_cmd_untrack", fake_untrack)
    assert cli.main(["detmux", "424242"]) == 0
    assert seen["pid"] == 424242
    # The primary name must still route the same way (aliases=[...] must not
    # have displaced "untrack" itself).
    assert cli.main(["untrack", "111111"]) == 0
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
    unit_names = (
        cli.systemd.SERVICE_NAME, cli.systemd.TIMER_NAME,
        cli.systemd.WEB_SERVICE_NAME, cli.systemd.AWAKE_SERVICE_NAME,
    )
    for name in unit_names:
        (ud / name).write_text("x")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    rc = cli.main(["systemd", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc == 0
    assert ["systemctl", "--user", "disable", "--now", cli.systemd.TIMER_NAME] in ran
    assert ["systemctl", "--user", "disable", "--now", cli.systemd.AWAKE_SERVICE_NAME] in ran
    assert not any((ud / n).exists() for n in unit_names)


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
    (ad / cli.launchd.AWAKE_PLIST).write_text("x")
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
    assert seen_at_unload[str(ad / cli.launchd.AWAKE_PLIST)] is True
    assert not (ad / cli.launchd.REVIVE_PLIST).exists()
    assert not (ad / cli.launchd.WEB_PLIST).exists()
    assert not (ad / cli.launchd.AWAKE_PLIST).exists()


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
    assert cli._tab_spawner(cfg.Config())[0] is None


def test_tab_spawner_selects_a_linux_terminal_on_a_desktop(monkeypatch):
    # Non-WSL Linux desktop with a display + an installed terminal.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.os, "environ", {"DISPLAY": ":0"})
    monkeypatch.setattr(cli.tab_spawn_linux.shutil, "which",
                        lambda b: "/usr/bin/kitty" if b == "kitty" else None)
    spawner, _expected = cli._tab_spawner(cfg.Config(overrides={"terminal": "kitty"}))
    assert isinstance(spawner, cli.tab_spawn_linux.LinuxTerminalSpawner)
    assert spawner.kind == "kitty"


def test_tab_spawner_selects_windows_terminal_under_wsl(monkeypatch):
    # WSL is checked before the Linux desktop path: wt.exe wins when present
    # AND the interop handler can exec it ([live bug, 2026-08-09]).
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows.shutil, "which",
                        lambda b: "/mnt/c/wt.exe" if b == "wt.exe" else None)
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows, "wt_probe", lambda path, timeout: True)
    spawner, _expected = cli._tab_spawner(cfg.Config())
    assert isinstance(spawner, cli.tab_spawn_windows.WindowsTerminalSpawner)


def test_tab_spawner_probe_false_does_not_run_the_wt_window_probe(monkeypatch):
    # The recovery paths (reopen / rescue re-home) pass probe=False so a
    # session coming back never flashes a wt help window
    # [/exit revival 2026-08-25]. wt_probe must not be called at all.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows.shutil, "which",
                        lambda b: "/mnt/c/wt.exe" if b == "wt.exe" else None)
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: True)

    def _boom(path, timeout):
        raise AssertionError("wt_probe (opens a GUI window) ran despite probe=False")

    monkeypatch.setattr(cli.tab_spawn_windows, "wt_probe", _boom)
    spawner, expected = cli._tab_spawner(cfg.Config(), probe=False)
    assert isinstance(spawner, cli.tab_spawn_windows.WindowsTerminalSpawner)
    assert expected is True


def test_tab_spawner_falls_through_when_wsl_interop_is_unregistered(monkeypatch):
    # wt.exe resolves on DrvFs but cannot exec — don't hand back a spawner
    # that will only ENOEXEC; fall through to the Linux desktop detector
    # (None when headless), so reopen prints the tmux attach fallback.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows.shutil, "which",
                        lambda b: "/mnt/c/wt.exe" if b == "wt.exe" else None)
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: False)
    monkeypatch.setattr(cli.tab_spawn_linux, "detect", lambda *a, **k: None)
    assert cli._tab_spawner(cfg.Config())[0] is None


def test_tab_spawner_is_none_on_other_platforms(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    spawner, tabs_expected = cli._tab_spawner(cfg.Config())
    assert spawner is None
    assert tabs_expected is False


# --- tabs_expected ([user request, 2026-08-09]) ---------------------------
#
# "the tab is not convenience — if I am clicking reopen I want the tab."
# A revival with no tab is only degraded on a host that HAS tabs; a headless
# box, an SSH session or a systemd timer can never open one, and flagging
# those every time would make the signal worthless. _tab_spawner is the only
# place that knows which is which, so it answers both questions at once.

def test_tabs_are_expected_on_wsl_even_when_the_spawner_is_unusable(monkeypatch):
    # The case the user hit: wt.exe resolves, interop is dead, no spawner.
    # Tabs are still EXPECTED here — that is exactly what makes it degraded
    # rather than "this host doesn't do tabs".
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows.shutil, "which", lambda b: "/mnt/c/wt.exe")
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: False)
    monkeypatch.setattr(cli.tab_spawn_linux, "detect", lambda *a, **k: None)
    spawner, tabs_expected = cli._tab_spawner(cfg.Config())
    assert spawner is None
    assert tabs_expected is True


def test_tabs_are_not_expected_on_headless_linux(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.tab_spawn_linux, "detect", lambda *a, **k: None)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    spawner, tabs_expected = cli._tab_spawner(cfg.Config())
    assert spawner is None
    assert tabs_expected is False


def test_tabs_are_expected_on_a_linux_desktop_with_no_terminal_installed(monkeypatch):
    # A graphical session where none of the known terminals is installed is a
    # real gap the user should see, not a headless box.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: False)
    monkeypatch.setattr(cli.tab_spawn_linux, "detect", lambda *a, **k: None)
    monkeypatch.setenv("DISPLAY", ":0")
    spawner, tabs_expected = cli._tab_spawner(cfg.Config())
    assert spawner is None
    assert tabs_expected is True


def test_cmd_reopen_warns_on_stderr_but_still_exits_zero_when_no_tab_opened(
    tmp_path, monkeypatch, capsys
):
    # [user request, 2026-08-09] The human needs to know the tab never came;
    # a script must not start reading a live session as a failure. So: loud
    # on stderr, exit 0.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, True))
    monkeypatch.setattr(cli.ops, "reopen",
                        lambda *a, **k: cli.ops.OpResult(True, "reopened 42 as crr-abc12345",
                                                          degraded=True))
    monkeypatch.setattr(cli.tmux, "RealTmux", lambda t: type("T", (), {"available": lambda s: True})())
    rc = cli.main(["reopen", "--pid", "42"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "reopened 42" in out
    assert "WARNING" in err and "no tab" in err.lower()


def test_cmd_reopen_stays_quiet_when_the_tab_opened(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, True))
    monkeypatch.setattr(cli.ops, "reopen",
                        lambda *a, **k: cli.ops.OpResult(True, "reopened 42 (opened in a new tab)"))
    monkeypatch.setattr(cli.tmux, "RealTmux", lambda t: type("T", (), {"available": lambda s: True})())
    assert cli.main(["reopen", "--pid", "42"]) == 0
    assert "WARNING" not in capsys.readouterr().err


def test_tabs_are_expected_on_macos(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.tab_spawn, "spawner_for",
                        lambda kind, timeout: type("S", (), {"available": lambda self: False})())
    spawner, tabs_expected = cli._tab_spawner(cfg.Config())
    assert spawner is None
    assert tabs_expected is True


def test_tab_spawner_selects_a_macos_spawner_when_app_present(monkeypatch):
    # On macOS, _tab_spawner returns the chosen spawner when its app is
    # installed (available() true), or None when it isn't.
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.tab_spawn.subprocess, "run",
                        lambda cmd, **kw: type("R", (), {"returncode": 0})())
    spawner, _expected = cli._tab_spawner(cfg.Config(overrides={"terminal": "iterm"}))
    assert isinstance(spawner, cli.tab_spawn.ITerm2Spawner)
    monkeypatch.setattr(cli.tab_spawn.subprocess, "run",
                        lambda cmd, **kw: type("R", (), {"returncode": 1})())
    assert cli._tab_spawner(cfg.Config())[0] is None


def test_doctor_reports_install_health(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "boot-identity adapter" in out
    assert "tmux" in out
    assert "state dir" in out
    assert "crr-revive.timer" in out  # names the watchdog unit to install


def test_doctor_prints_all_six_declared_contract_versions(tmp_path, monkeypatch, capsys):
    # F8: doctor used to print 3 of 6 declared contract versions (omitting
    # archive, config-defaults, page) — an honesty gap the audit flagged.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"journal v{contracts.JOURNAL_SCHEMA_VERSION}" in out
    assert f"sessions v{contracts.SESSIONS_CONTRACT_VERSION}" in out
    assert f"diagnostics v{contracts.DIAGNOSTICS_CONTRACT_VERSION}" in out
    assert f"archive v{contracts.ARCHIVE_CONTRACT_VERSION}" in out
    assert f"config-defaults v{cfg.CONFIG_DEFAULTS_VERSION}" in out
    assert f"page v{cli.web.PAGE_VERSION}" in out


def test_doctor_reports_no_tab_spawn_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "tab spawn" in out
    assert "not yet exercised" in out


def test_doctor_reports_the_aumid_tier_with_the_alias_note(tmp_path, monkeypatch, capsys):
    import json
    from crr.core import contracts, tab_health
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / tab_health.FILENAME).write_text(json.dumps({
        "v": contracts.TAB_HEALTH_STORE_VERSION,
        "tier": tab_health.TIER_AUMID, "detail": "",
        "ts": "2026-08-29T12:00:00Z", "boot_id": "b1",
    }), encoding="utf-8")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "app package" in out
    assert "App execution aliases" in out
    assert "2026-08-29T12:00:00Z" in out


def test_doctor_warns_when_no_launcher_worked(tmp_path, monkeypatch, capsys):
    import json
    from crr.core import contracts, tab_health
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / tab_health.FILENAME).write_text(json.dumps({
        "v": contracts.TAB_HEALTH_STORE_VERSION,
        "tier": tab_health.TIER_NONE, "detail": "everything failed",
        "ts": "2026-08-29T12:00:00Z", "boot_id": "b1",
    }), encoding="utf-8")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "everything failed" in out


def test_config_effective_lists_every_key_with_origin(capsys):
    rc = cli.main(["config", "--effective"])
    out = capsys.readouterr().out
    assert rc == 0
    for key in cfg.DEFAULTS:
        assert key in out
    assert "(default)" in out


def test_config_effective_prints_defaults_version_header(capsys):
    # F9: --effective never printed CONFIG_DEFAULTS_VERSION (zero consumers
    # could tell which default generation they were reading).
    rc = cli.main(["config", "--effective"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == f"# defaults version: {cfg.CONFIG_DEFAULTS_VERSION}"


def _live_entry(pid, boot_id):
    return {
        "v": contracts.JOURNAL_SCHEMA_VERSION,
        "pid": pid,
        "boot_id": boot_id,
        "cwd": "/home/u/project",
        "host": "tmux",
        "shell": "zsh",
        "claude": {
            "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "sid_source": "injected",
            "started": "2026-07-23T00:00:00Z",
            "skip_permissions": False,
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


# --------------------------------------------------------------------------
# The reachability detector on the CARD path (spec 2026-08-09, Phase 3).
# `_kick_dropped_bridges` already reads Claude Code's own state file; these
# pin the same source behind `crr status --json` / the web provider /
# `crr whoami`, and pin the cost of doing so.
# --------------------------------------------------------------------------

_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _no_tail_facts(monkeypatch):
    """Silence the transcript read — these tests are about the state file."""
    monkeypatch.setattr(
        cli.transcript_source, "read_tail_facts",
        lambda sid, cap, **kw: {
            "last_prompt": "", "model": "", "last_active": "", "last_reply": "",
            "title": "", "slug": "", "transcript_bytes": 0,
        },
    )


def _state(bridge_session_id, *, pid, field_present=True,
           status="waiting", waiting_for="permission prompt"):
    return session_state.SessionState(
        pid=pid, bridge_session_id=bridge_session_id,
        field_present=field_present, status=status, waiting_for=waiting_for,
    )


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter (Linux or macOS)")
def test_status_json_reports_reachability_from_the_state_file(tmp_path, monkeypatch, capsys):
    # End to end through the composition root: the card's remote_control
    # comes from Claude Code's own `bridgeSessionId`, not from counting
    # transcript records.
    boot_id = boot_identity.detect().current()
    shell_pid = os.getpid()
    JournalStore(tmp_path).write(_live_entry(pid=shell_pid, boot_id=boot_id))
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _no_tail_facts(monkeypatch)
    monkeypatch.setattr(cli.session_state, "read_all",
                        lambda: {_SID: _state(None, pid=4242)})
    monkeypatch.setattr(process_probe.PsProcessProbe, "claude_group_pids",
                        lambda self, pids: {shell_pid: [4242]})

    assert cli.main(["status", "--json"]) == 0
    card = json.loads(capsys.readouterr().out)["sessions"][0]
    assert card["remote_control"] == "unreachable"
    assert card["waiting_for"] == "permission prompt"


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter (Linux or macOS)")
def test_status_json_declines_to_believe_a_state_file_whose_pid_is_not_ours(tmp_path, monkeypatch, capsys):
    # 117 of 133 state files on the author's machine belonged to dead pids
    # and 2 to RECYCLED ones. An unmatched pid is `unknown` — and its
    # `waitingFor` is exactly as untrustworthy as its bridge id, so the
    # card must not carry it either.
    boot_id = boot_identity.detect().current()
    JournalStore(tmp_path).write(_live_entry(pid=os.getpid(), boot_id=boot_id))
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _no_tail_facts(monkeypatch)
    monkeypatch.setattr(cli.session_state, "read_all",
                        lambda: {_SID: _state(None, pid=4242)})
    monkeypatch.setattr(process_probe.PsProcessProbe, "claude_group_pids",
                        lambda self, pids: {})

    assert cli.main(["status", "--json"]) == 0
    card = json.loads(capsys.readouterr().out)["sessions"][0]
    assert card["remote_control"] == "unknown"
    assert card["waiting_for"] == ""


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter (Linux or macOS)")
def test_status_json_probes_the_process_table_once_for_all_cards(tmp_path, monkeypatch, capsys):
    # The dashboard polls every 5s. A per-card `ps` snapshot would cost
    # ~204 forks/minute at the 17 cards measured on the author's machine,
    # forever. One batched query per poll, exactly like `controlling_ttys`.
    boot_id = boot_identity.detect().current()
    store = JournalStore(tmp_path)
    store.write(_live_entry(pid=os.getpid(), boot_id=boot_id))
    for pid, sid in ((424242, "11111111-1111-4111-8111-111111111111"),
                     (424243, "22222222-2222-4222-8222-222222222222")):
        entry = _live_entry(pid=pid, boot_id=boot_id)
        entry["claude"]["session_id"] = sid
        store.write(entry)
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _no_tail_facts(monkeypatch)
    monkeypatch.setattr(cli.session_state, "read_all",
                        lambda: {_SID: _state("bridge-abc", pid=4242)})
    calls = []
    monkeypatch.setattr(
        process_probe.PsProcessProbe, "claude_group_pids",
        lambda self, pids: (calls.append(list(pids)), {})[1],
    )

    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["sessions"]) == 3
    assert len(calls) == 1, f"one snapshot per poll, not one per card: {calls}"
    assert sorted(calls[0]) == sorted([os.getpid(), 424242, 424243])


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"), reason="needs the boot-identity adapter (Linux or macOS)")
def test_status_json_remote_control_is_unknown_when_watch_is_disabled(tmp_path, monkeypatch, capsys):
    # End-to-end through `crr status --json`: with `remote_control_watch`
    # off the card reads "unknown", never a positive claim — even though a
    # readable state file says the bridge is up. Turning the watch off asks
    # crr to stop looking; the honest report of not looking is "unknown".
    boot_id = boot_identity.detect().current()
    JournalStore(tmp_path).write(_live_entry(pid=os.getpid(), boot_id=boot_id))
    (tmp_path / "config.toml").write_text("remote_control_watch = false\n", encoding="utf-8")
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _no_tail_facts(monkeypatch)
    monkeypatch.setattr(cli.session_state, "read_all",
                        lambda: {_SID: _state("bridge-abc", pid=os.getpid())})

    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sessions"][0]["remote_control"] == "unknown"


def test_reachability_lookup_is_skipped_entirely_when_watch_is_off():
    # Review fix-wave 2026-08-07, FIX 2 (IMPORTANT), restated for the new
    # detector: `remote_control_watch` gates the READ, not just the badge.
    # With it off, neither `~/.claude/sessions` nor the process table is
    # touched — no cost is paid on every 5s poll for a feature that is off.
    entries = [{"pid": 1, "claude": {"session_id": _SID}}]

    def boom(*a, **kw):
        raise AssertionError("watch is off — nothing should have been read")

    probe = SimpleNamespace(claude_group_pids=boom)
    assert cli._reachability_by_sid(
        entries, probe, cfg.Config({"remote_control_watch": False}),
        read_session_state=boom,
    ) == {}


def test_reachability_lookup_reads_when_watch_is_on():
    entries = [{"pid": 1, "claude": {"session_id": _SID}}]
    probe = SimpleNamespace(claude_group_pids=lambda pids: {1: [77]})
    got = cli._reachability_by_sid(
        entries, probe, cfg.Config({"remote_control_watch": True}),
        read_session_state=lambda: {_SID: _state(None, pid=77)},
    )
    assert got == {_SID: ("unreachable", "permission prompt")}


def _human_card(model, duplicate_group=None, sid_source="injected"):
    return {
        "pid": 42, "sid8": "8a1b2c3d", "state": "live", "cwd": "/home/u/proj",
        "model": model, "duplicate_group": duplicate_group, "sid_source": sid_source,
    }


def test_status_human_groups_worktrees_under_their_repo(capsys):
    # #31: a worktree session is demoted below the main threads into a
    # per-repo section and tagged with its worktree name.
    main = _human_card("", )
    main["cwd"] = "/home/u/proj"
    wt = _human_card("")
    wt["pid"] = 99
    wt["cwd"] = "/home/u/proj/.claude/worktrees/feature-x"
    cli._print_status_human({"sessions": [wt, main]})  # worktree first on input
    out = capsys.readouterr().out
    # Main thread prints before the worktree section.
    assert out.index("#42") < out.index("/home/u/proj · worktrees (1)")
    assert out.index("/home/u/proj · worktrees (1)") < out.index("#99")
    assert "[worktree:feature-x]" in out


def test_status_human_shows_model_when_known(capsys):
    cli._print_status_human({"sessions": [_human_card("claude-opus-5")]})
    assert "claude-opus-5" in capsys.readouterr().out


def test_status_human_omits_model_when_unknown(capsys):
    # No model read yet -> the line is the plain terse form, no trailing gap.
    # This also pins F13's compactness requirement: an `injected` sid_ource
    # (the certain norm) adds nothing to the line.
    cli._print_status_human({"sessions": [_human_card("")]})
    assert capsys.readouterr().out == "#42 · 8a1b2c3d [live] /home/u/proj\n"


def test_status_human_dup_tag_for_certain_sid(capsys):
    # F13: verified/injected duplicates print the plain certain tag.
    card = _human_card("", duplicate_group="8a1b2c3d-...", sid_source="verified")
    cli._print_status_human({"sessions": [card]})
    out = capsys.readouterr().out
    assert "[dup]" in out
    assert "[dup?" not in out
    assert "sid:verified" in out  # non-injected sid_source is always surfaced


def test_status_human_dup_tag_qualifies_guessed_sid(capsys):
    # F13: a guessed duplicate must not collapse into the same [dup] tag as
    # a certain one — sid_source travels with the human line too.
    card = _human_card("", duplicate_group="8a1b2c3d-...", sid_source="guessed")
    cli._print_status_human({"sessions": [card]})
    out = capsys.readouterr().out
    assert "[dup? guessed]" in out
    assert "sid:guessed" in out


def test_status_human_shows_sid_source_when_not_injected_even_without_dup(capsys):
    # F13: sid_source is dropped even for non-duplicate guessed/verified
    # cards today — the dashboard renders the distinction, the CLI doesn't.
    card = _human_card("", duplicate_group=None, sid_source="guessed")
    cli._print_status_human({"sessions": [card]})
    out = capsys.readouterr().out
    assert "[dup" not in out
    assert "sid:guessed" in out


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


def _claude_field(sid="8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", skip_permissions=False):
    return {"session_id": sid, "sid_source": "injected", "started": "2026-07-24T00:00:00Z",
            "skip_permissions": skip_permissions}


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


def test_deregister_archives_claude_bearing_entry(tmp_path, monkeypatch):
    """A shell exit must not silently destroy a claude session's revival data (#99)."""
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _seed(store, 4242)
    entry = store.read(4242)
    entry["claude"] = _claude_field(sid)
    store.write(entry)
    assert cli.main(["deregister", "--pid", "4242"]) == 0
    assert not store.tabs_dir.joinpath("4242.json").exists()
    archive = ArchiveStore(tmp_path)
    record = archive.read(sid)
    assert record["reason"] == "shell-exited"


def test_deregister_reason_closed_archives_terminally(tmp_path, monkeypatch):
    """The reviver's exit-hook wrapper passes --reason closed on a clean /exit
    so the conversation is archived under a TERMINAL reason (not the revivable
    'shell-exited'), and the next revive pass leaves it alone [/exit revival 2026-08-24]."""
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _seed(store, 4242)
    entry = store.read(4242)
    entry["claude"] = _claude_field(sid)
    store.write(entry)
    assert cli.main(["deregister", "--pid", "4242", "--reason", "closed"]) == 0
    assert not store.tabs_dir.joinpath("4242.json").exists()
    assert ArchiveStore(tmp_path).read(sid)["reason"] == "closed"


def test_deregister_invalidates_rescue_markers_on_archive(tmp_path, monkeypatch):
    """When deregister archives a claude-bearing entry, it must clear rescue
    markers so the next shell startup re-scans and offers the just-closed
    session instead of staying silent behind an already-prompted gate."""
    from crr.core import rescue
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _seed(store, 4242)
    entry = store.read(4242)
    entry["claude"] = _claude_field(sid)
    store.write(entry)
    rescue.claim_prompt(tmp_path, "current-boot")
    rescue.mark_revived(tmp_path, "current-boot")
    assert rescue.already_prompted(tmp_path, "current-boot")

    assert cli.main(["deregister", "--pid", "4242"]) == 0

    assert not rescue.already_prompted(tmp_path, "current-boot")
    assert not rescue.already_revived(tmp_path, "current-boot")


def test_deregister_invalidates_markers_for_claude_less_too(tmp_path, monkeypatch):
    """A plain shell exit also clears rescue markers — the user's claude
    sessions live in tmux, not in Terminal shells, so a claude-less shell
    closing is the normal trigger for 'Terminal was closed, re-scan.'"""
    from crr.core import rescue
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    rescue.claim_prompt(tmp_path, "current-boot")
    assert rescue.already_prompted(tmp_path, "current-boot")

    assert cli.main(["deregister", "--pid", "4242"]) == 0

    assert not rescue.already_prompted(tmp_path, "current-boot")


def test_deregister_no_archive_for_claude_less_entry(tmp_path, monkeypatch):
    """A plain shell exit (no claude) needs no archive record."""
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    assert cli.main(["deregister", "--pid", "4242"]) == 0
    archive = ArchiveStore(tmp_path)
    assert archive.scan().records == []


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


def test_claude_launch_records_skip_permissions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    cli.main(["claude-launch", "--pid", "4242", "--skip-permissions"])
    claude = store.read(4242)["claude"]
    assert claude["skip_permissions"] is True


def test_claude_launch_defaults_skip_permissions_false(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242)
    cli.main(["claude-launch", "--pid", "4242"])
    claude = store.read(4242)["claude"]
    assert claude["skip_permissions"] is False


def test_claude_resume_records_skip_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    sid = "eeeeeeee-5555-4555-8555-555555555555"
    _write_transcript_file(tmp_path / "home", "/home/u/proj", sid)
    cli.main(["claude-resume", "--pid", "4242", "--cwd", "/home/u/proj",
              "--session-id", sid, "--skip-permissions"])
    claude = store.read(4242)["claude"]
    assert claude["skip_permissions"] is True


def test_claude_resume_defaults_skip_permissions_false(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    sid = "eeeeeeee-5555-4555-8555-555555555555"
    _write_transcript_file(tmp_path / "home", "/home/u/proj", sid)
    cli.main(["claude-resume", "--pid", "4242", "--cwd", "/home/u/proj",
              "--session-id", sid])
    claude = store.read(4242)["claude"]
    assert claude["skip_permissions"] is False


def test_claude_resume_without_flag_clears_prior_skip_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    sid = "eeeeeeee-5555-4555-8555-555555555555"
    _write_transcript_file(tmp_path / "home", "/home/u/proj", sid)
    cli.main(["claude-resume", "--pid", "4242", "--cwd", "/home/u/proj",
              "--session-id", sid, "--skip-permissions"])
    assert store.read(4242)["claude"]["skip_permissions"] is True
    cli.main(["claude-resume", "--pid", "4242", "--cwd", "/home/u/proj",
              "--session-id", sid])
    assert store.read(4242)["claude"]["skip_permissions"] is False


# --- claude() wrapper support: remote-control-args (shim-facing) ---------

def test_remote_control_args_prints_the_flag_and_derived_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store = JournalStore(tmp_path)
    _seed(store, 4242, cwd="/home/u/my project")
    rc = cli.main(["remote-control-args", "--pid", "4242"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == ["--remote-control", "my-project"]


def test_remote_control_args_is_empty_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    (tmp_path / "config.toml").write_text("remote_control = false\n", encoding="utf-8")
    store = JournalStore(tmp_path)
    _seed(store, 4242, cwd="/home/u/my-project")
    rc = cli.main(["remote-control-args", "--pid", "4242"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_remote_control_args_is_empty_when_untracked(tmp_path, monkeypatch, capsys):
    # No entry journaled for this pid: a shim-facing command must degrade
    # to silence, never an error into the prompt.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["remote-control-args", "--pid", "999"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


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
    set_home(monkeypatch, str(tmp_path / "home"))
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
    set_home(monkeypatch, str(tmp_path / "home"))
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
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    rc = cli.main(["claude-resume", "--pid", "4242", "--cwd", "/home/u/proj"])
    assert rc == 0
    assert store.read(4242)["claude"] is None  # nothing to guess -> untracked


_G1_SID = "11112222-3333-4444-5555-666677778888"


def test_verify_guessed_sids_upgrades_when_transcript_is_active(tmp_path, monkeypatch):
    # The revive-sweep helper upgrades a guessed sid to verified once its
    # transcript shows activity after the session started.
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    entry = new_entry(
        pid=7, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="b", now="2026-07-25T12:00:00+00:00",
        claude={"session_id": _G1_SID, "sid_source": "guessed",
                "started": "2026-07-25T12:00:00+00:00",
                "skip_permissions": False},
    )
    store.write(entry)
    started = datetime.fromisoformat("2026-07-25T12:00:00+00:00").timestamp()
    _write_transcript_file(tmp_path / "home", "/home/u/proj", _G1_SID, mtime=started + 60)

    cli._verify_guessed_sids(store, "2026-07-25T12:05:00+00:00")
    assert store.read(7)["claude"]["sid_source"] == "verified"


def test_verify_guessed_sids_leaves_idle_guess_alone(tmp_path, monkeypatch):
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    entry = new_entry(
        pid=7, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="b", now="2026-07-25T12:00:00+00:00",
        claude={"session_id": _G1_SID, "sid_source": "guessed",
                "started": "2026-07-25T12:00:00+00:00",
                "skip_permissions": False},
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
                "started": "2026-07-30T00:00:00+00:00",
                "skip_permissions": False},
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
                "started": "2026-07-30T00:00:00+00:00",
                "skip_permissions": False},
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
    set_home(monkeypatch, str(tmp_path / "home"))

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return set()  # nothing live -> the crashed entry is revived

        def attached_sessions(self):
            return set()  # nothing attached (#32)

        def new_detached_session(self, name, cwd, argv):
            pass

        def session_pid(self, name):
            return None  # unknown: no re-key (#58)

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)

    store = JournalStore(tmp_path / "state")
    store.write(new_entry(
        pid=7, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="an-old-boot-that-cannot-match",  # boot mismatch => crashed
        now="2026-07-25T12:00:00+00:00",
        claude={"session_id": _G1_SID, "sid_source": "guessed",
                "started": "2026-07-25T12:00:00+00:00",
                "skip_permissions": False},
    ))
    started = datetime.fromisoformat("2026-07-25T12:00:00+00:00").timestamp()
    _write_transcript_file(tmp_path / "home", "/home/u/proj", _G1_SID, mtime=started + 60)

    assert cli.main(["revive"]) == 0
    entry = store.read(7)
    assert entry["claude"]["sid_source"] == "verified"  # upgrade survived revive's write
    assert entry["tmux_session"] == f"crr-{_G1_SID}"  # and it was actually revived

    def session_pid(self, name):
        return None  # unknown: no re-key (#58)


@pytest.mark.skipif(platform.system() not in ("Linux", "Darwin"),
                     reason="needs the boot-identity adapter (Linux or macOS)")
def test_revive_names_gave_up_pids(tmp_path, monkeypatch, capsys):
    # F14: a terminal outcome (gave up) used to report a bare count while
    # sibling problem-loops name the file/session responsible.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return set()

        def new_detached_session(self, name, cwd, argv):
            pass

        def session_pid(self, name):
            return None  # unknown: no re-key (#58)

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)
    monkeypatch.setattr(
        cli.reviver, "revive_crashed",
        lambda *a, **k: cli.reviver.RevivalOutcome([], [4242], []),
    )
    rc = cli.main(["revive"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gave up: [4242]" in out


def test_revive_omits_gave_up_line_when_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return set()

        def new_detached_session(self, name, cwd, argv):
            pass

        def session_pid(self, name):
            return None  # unknown: no re-key (#58)

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)
    monkeypatch.setattr(
        cli.reviver, "revive_crashed",
        lambda *a, **k: cli.reviver.RevivalOutcome([], [], []),
    )
    rc = cli.main(["revive"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "revived 0" in out  # normal-path summary line is still printed
    assert "gave up:" not in out


def test_revive_reports_skipped_tmux_state_and_omits_summary(tmp_path, monkeypatch, capsys):
    # Finding 1 (re-audit): a None-liveness pass used to print the exact
    # same "revived 0, gave up 0, already running 0" summary as a genuine
    # no-op pass — a success-shaped line lying about an unknown state. Now
    # RevivalOutcome.skipped surfaces the distinction and the CLI must
    # honor it: a stderr note instead of the summary, but still exit 0 (a
    # flapping nonzero oneshot would spam systemd failure state under a
    # transient fault).
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return set()

        def new_detached_session(self, name, cwd, argv):
            pass

        def session_pid(self, name):
            return None  # unknown: no re-key (#58)

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)
    monkeypatch.setattr(
        cli.reviver, "revive_crashed",
        lambda *a, **k: cli.reviver.RevivalOutcome([], [], [], skipped=True),
    )
    rc = cli.main(["revive"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == ""  # no success-shaped summary line for a skipped pass
    assert "crr revive: tmux state unknown — pass skipped (no strikes accrued)" in err


def test_revive_skips_revival_when_auth_expired(tmp_path, monkeypatch, capsys):
    """When OAuth credentials are expired, the entire revival pass must be
    skipped — a revived session would launch under the stale token, die
    immediately, and burn a give-up strike it can never recover from.
    """
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    creds_path = tmp_path / ".credentials.json"
    now = 1_700_000_000.0
    creds_path.write_text(json.dumps({
        "expiresAt": int((now - 3600) * 1000),
        "refreshTokenExpiresAt": int((now - 7200) * 1000),
    }))
    monkeypatch.setattr(cli, "_credentials_path", lambda _cfg: creds_path)
    monkeypatch.setattr(cli.time, "time", lambda: now)

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return set()

        def new_detached_session(self, name, cwd, argv):
            pass

        def session_pid(self, name):
            return None

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)
    revive_called = []
    monkeypatch.setattr(
        cli.reviver, "revive_crashed",
        lambda *a, **k: revive_called.append(True) or cli.reviver.RevivalOutcome([], [], []),
    )

    rc = cli.main(["revive"])
    assert rc == 0
    assert revive_called == [], "revive_crashed must NOT be called when auth is expired"
    err = capsys.readouterr().err
    assert "auth expired" in err
    assert "skipping revival" in err.lower()


def test_revive_proceeds_when_auth_valid(tmp_path, monkeypatch, capsys):
    """Revival must proceed normally when auth is valid."""
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    creds_path = tmp_path / ".credentials.json"
    now = 1_700_000_000.0
    creds_path.write_text(json.dumps({
        "expiresAt": int((now + 4 * 86400) * 1000),
        "refreshTokenExpiresAt": int((now + 30 * 86400) * 1000),
    }))
    monkeypatch.setattr(cli, "_credentials_path", lambda _cfg: creds_path)
    monkeypatch.setattr(cli.time, "time", lambda: now)

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return set()

        def new_detached_session(self, name, cwd, argv):
            pass

        def session_pid(self, name):
            return None

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)
    revive_called = []
    monkeypatch.setattr(
        cli.reviver, "revive_crashed",
        lambda *a, **k: revive_called.append(True) or cli.reviver.RevivalOutcome([], [], []),
    )

    rc = cli.main(["revive"])
    assert rc == 0
    assert revive_called == [True], "revive_crashed must be called when auth is valid"


def test_revive_invokes_the_bridge_watchdog_pass_after_the_summary(tmp_path, monkeypatch, capsys):
    # Slice 2 (dropped-Remote-Control watchdog): the deliverable is the
    # WIRING in `_cmd_revive`, not just the standalone `_kick_dropped_bridges`
    # helper (that is covered exhaustively, with fakes, in
    # test_revive_bridge.py). Pin here that `crr revive` actually calls it
    # exactly once, with a JournalStore and `sd` rooted at the real state
    # dir, and only AFTER the crashed-session summary line — so deleting the
    # call site, or wiring the wrong sd/store, fails a test.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    class _FakeTmux:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def list_sessions(self):
            return set()

        def new_detached_session(self, name, cwd, argv):
            pass

        def session_pid(self, name):
            return None  # unknown: no re-key (#58)

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)

    calls = []

    def fake_watchdog(entries, boot, probe, config, settings_store, store, sd, controller, flags,
                       *, credentials_path=None):
        calls.append((sd, store, credentials_path))
        print("watchdog pass ran")

    monkeypatch.setattr(cli, "_kick_dropped_bridges", fake_watchdog)

    rc = cli.main(["revive"])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 1
    sd, store, credentials_path = calls[0]
    assert sd == tmp_path
    assert isinstance(store, JournalStore)
    # [dashboard reauth] the watchdog must see the same credentials path the
    # dashboard poll reads, so a systemd-timer sweep and a browser poll agree
    # on whether auth is expired.
    assert credentials_path == cli._credentials_path(cfg.Config())
    lines = out.splitlines()
    assert lines.index("revived 0, gave up 0, already running 0") < lines.index("watchdog pass ran")


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


def test_gc_names_removed_sid8s(tmp_path, monkeypatch, capsys):
    # F14: gc reported a bare count while sibling problem-loops name files.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    archive = ArchiveStore(tmp_path)
    old = _live_entry(pid=1, boot_id="b")
    old["claude"] = _claude_field("11111111-1111-4111-8111-111111111111")
    archive.archive(old, "gave-up", "2026-01-01T00:00:00+00:00")
    assert cli.main(["gc"]) == 0
    out = capsys.readouterr().out
    assert "removed: ['11111111']" in out


def test_gc_omits_removed_line_when_nothing_removed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    ArchiveStore(tmp_path)  # empty archive dir
    assert cli.main(["gc"]) == 0
    out = capsys.readouterr().out
    assert "removed:" not in out


# --- F15: `crr archive --list` — the human read path archive lineage lacked

def test_archive_list_reports_none_when_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["archive", "--list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "no archived sessions"


def test_archive_list_prints_one_line_per_record_sorted_by_archived_at_desc(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    archive = ArchiveStore(tmp_path)
    old = _live_entry(pid=1, boot_id="b")
    old["claude"] = _claude_field("11111111-1111-4111-8111-111111111111")
    old["cwd"] = "/home/u/old"
    fresh = _live_entry(pid=2, boot_id="b")
    fresh["claude"] = _claude_field("22222222-2222-4222-8222-222222222222")
    fresh["cwd"] = "/home/u/fresh"
    archive.archive(old, "gave-up", "2026-01-01T00:00:00+00:00")
    archive.archive(fresh, "dismissed", "2026-06-01T00:00:00+00:00")

    rc = cli.main(["archive", "--list"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.strip().splitlines()
    assert len(lines) == 2
    # most-recently-archived first
    assert lines[0].startswith("dismissed")
    assert "22222222" in lines[0] and "/home/u/fresh" in lines[0] and "2026-06-01" in lines[0]
    assert lines[1].startswith("gave-up")
    assert "11111111" in lines[1] and "/home/u/old" in lines[1] and "2026-01-01" in lines[1]


def test_archive_requires_the_list_flag(capsys):
    rc = cli.main(["archive"])
    assert rc == 2
    assert "usage: crr archive --list" in capsys.readouterr().err


def test_archive_list_surfaces_scan_problems_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "corrupt.json").write_text("not json", encoding="utf-8")
    rc = cli.main(["archive", "--list"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "corrupt.json" in err
    assert "no archived sessions" in out


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
        assert store.read(42)["tmux_session"] == f"crr-{sid}"
        sessions = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        ).stdout
        assert f"crr-{sid}" in sessions
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


def test_reopen_headless_drops_you_into_tmux(tmp_path, monkeypatch, capsys):
    # On a headless host, `crr reopen <pid>` reopens the parked session AND
    # runs the terminal primitive on it (attach if not in tmux) rather than
    # just printing a message.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)  # available(), list_sessions()
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))  # headless
    monkeypatch.delenv("TMUX", raising=False)  # not in tmux
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=42, cwd="/home/u/alpha", host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid), tmux_session="crr-8a1b2c3d"))

    # ops.reopen succeeds (parked entry stays tracked); stub it to avoid real tmux.
    monkeypatch.setattr(cli.ops, "reopen",
                        lambda *a, **k: SimpleNamespace(ok=True, degraded=False,
                                                        message="reopened 42 as crr-8a1b2c3d"))
    execed = []
    monkeypatch.setattr(cli, "_exec", lambda file, argv: execed.append((file, argv)))

    rc = cli.main(["reopen", "--pid", "42"])
    assert rc == 0
    assert execed == [("tmux", ["tmux", "attach", "-t", "crr-8a1b2c3d"])]


def test_reopen_headless_ghost_still_drops_you_in(tmp_path, monkeypatch, capsys):
    # A GHOST reopen delists the entry (store.remove) before returning ok.
    # The drop-in must still fire — the name is captured BEFORE reopen.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=42, cwd="/home/u/alpha", host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid), tmux_session="crr-8a1b2c3d"))

    def fake_reopen(*a, **k):
        JournalStore(tmp_path).remove(42)  # ghost delist
        return SimpleNamespace(ok=True, degraded=False,
                               message="ghost-restored 42 as crr-8a1b2c3d")
    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)
    execed = []
    monkeypatch.setattr(cli, "_exec", lambda file, argv: execed.append((file, argv)))

    rc = cli.main(["reopen", "--pid", "42"])
    assert rc == 0
    assert execed == [("tmux", ["tmux", "attach", "-t", "crr-8a1b2c3d"])]


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
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))

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

    opened = []

    class _FakeTab:
        def available(self):
            return True

        def open_tab(self, argv, cwd=None):
            opened.append((list(argv), cwd))

    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmux)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))

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
    # Relaunched through a shimmed interactive shell (#33), not bare claude —
    # so the new window self-registers and crr keeps tracking it.
    assert opened == [(["zsh", "-i", "-c", f"claude --resume {_SID}"], str(tmp_path))]
    out = capsys.readouterr().out
    assert "un-tmuxed" in out
    assert "crr still manages it" in out
    assert "crr no longer manages it" not in out


def test_untracked_view_reads_last_prompt_from_the_transcript(tmp_path, monkeypatch):
    # A journal entry (what an archive record wraps) never carries last_prompt,
    # but the untracked session's transcript is still on disk — so the retrack
    # panel reads the real last prompt from it (parity with the discoverable
    # panel), rather than omitting the field. Lazy panel only, never the poll.
    sid = "eeeeeeee-5555-4555-8555-555555555555"
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/p42", sid, [
        _discover_user_rec("the last thing I typed", cwd="/p42"),
    ])
    entry = new_entry(
        pid=42, cwd="/p42", host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid),
    )
    record = {"entry": entry, "reason": "untracked", "archived_at": "2026-08-01T00:00:00+00:00"}
    view = cli._untracked_view(record, cap=80, model_tail_lines=40)
    assert view["last_prompt"] == "the last thing I typed"
    assert set(view) == {"session_id", "sid8", "cwd", "archived_at", "last_prompt"}


def test_untracked_view_last_prompt_empty_when_transcript_absent(tmp_path, monkeypatch):
    # A gone/unreadable transcript degrades to "" — honest empty, never an error.
    sid = "eeeeeeee-5555-4555-8555-555555555555"
    set_home(monkeypatch, str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    entry = new_entry(
        pid=42, cwd="/p42", host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid),
    )
    record = {"entry": entry, "reason": "untracked", "archived_at": "2026-08-01T00:00:00+00:00"}
    view = cli._untracked_view(record, cap=80, model_tail_lines=40)
    assert view["last_prompt"] == ""


# --- retrack (C2) — undo untrack/detmux, no platform gating needed --------

def _archived_untracked(archive, pid, sid, reason="untracked", archived_at="2026-08-01T00:00:00+00:00"):
    entry = new_entry(
        pid=pid, cwd=f"/p{pid}", host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid),
    )
    archive.archive(entry, reason, archived_at)
    return entry


def test_retrack_by_sid_restores_the_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    sid = "eeeeeeee-5555-4555-8555-555555555555"
    _archived_untracked(archive, 42, sid)

    rc = cli.main(["retrack", "--sid", sid])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"retracked {sid[:8]}" in out
    assert store.read(42)["claude"]["session_id"] == sid
    with pytest.raises(KeyError):
        archive.read(sid)


def test_retrack_by_sid_rejects_a_malformed_sid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["retrack", "--sid", "not-a-uuid"])
    assert rc == 2
    assert "not a valid session id" in capsys.readouterr().err


def test_retrack_by_sid_reports_a_missing_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "eeeeeeee-5555-4555-8555-555555555555"
    rc = cli.main(["retrack", "--sid", sid])
    assert rc == 1
    assert "no archived session" in capsys.readouterr().err


def test_retrack_last_defaults_to_ten_most_recent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    sids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(12)]
    for i, sid in enumerate(sids):
        _archived_untracked(archive, i, sid, archived_at=f"2026-08-01T00:00:{i:02d}+00:00")

    rc = cli.main(["retrack"])
    assert rc == 0
    out = capsys.readouterr().out
    # The 10 most recently archived (highest index) were retracked; the two
    # oldest (index 0, 1) are left behind.
    for sid in sids[2:]:
        assert store.read(sids.index(sid))["claude"]["session_id"] == sid
    for sid in sids[:2]:
        assert archive.read(sid)["reason"] == "untracked"
    assert out.count("retracked ") == 10


def test_retrack_last_n_retracks_only_the_most_recent_n(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    old_sid = "10000000-0000-4000-8000-000000000000"
    new_sid = "20000000-0000-4000-8000-000000000000"
    _archived_untracked(archive, 1, old_sid, archived_at="2026-08-01T00:00:00+00:00")
    _archived_untracked(archive, 2, new_sid, archived_at="2026-08-01T00:00:01+00:00")

    rc = cli.main(["retrack", "--last", "1"])
    assert rc == 0
    assert store.read(2)["claude"]["session_id"] == new_sid  # the more recent one
    with pytest.raises(KeyError):
        store.read(1)
    assert archive.read(old_sid)["reason"] == "untracked"  # left behind


def test_retrack_reports_nothing_to_do_when_archive_is_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["retrack"])
    assert rc == 0
    assert "no untracked sessions to retrack" in capsys.readouterr().out


def test_retrack_ignores_non_untracked_archive_records(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    archive = ArchiveStore(tmp_path)
    sid = "33333333-3333-4333-8333-333333333333"
    _archived_untracked(archive, 1, sid, reason="dismissed")

    rc = cli.main(["retrack"])
    assert rc == 0
    assert "no untracked sessions to retrack" in capsys.readouterr().out
    assert archive.read(sid)["reason"] == "dismissed"  # untouched


def test_retrack_rejects_sid_combined_with_last(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "44444444-4444-4444-8444-444444444444"
    rc = cli.main(["retrack", "--sid", sid, "--last", "5"])
    assert rc == 2
    assert "--sid cannot be combined with --last" in capsys.readouterr().err


def test_retrack_rejects_a_negative_last(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    rc = cli.main(["retrack", "--last", "-1"])
    assert rc == 2
    assert "--last must not be negative" in capsys.readouterr().err


def test_retrack_surfaces_scan_problems_on_stderr(tmp_path, monkeypatch, capsys):
    # Mirrors test_archive_list_surfaces_scan_problems_on_stderr: a corrupt
    # archive file must be reported, never silently dropped from the count.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "corrupt.json").write_text("not json", encoding="utf-8")

    rc = cli.main(["retrack"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "corrupt.json" in err
    assert "no untracked sessions to retrack" in out


# --- discover (T-C, C3) — surface + adopt untracked transcripts -----------


def _write_discover_transcript(home, cwd, sid, records):
    d = home / ".claude" / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _discover_user_rec(text, cwd=None, **extra):
    rec = {"type": "user", "message": {"role": "user", "content": text}}
    if cwd is not None:
        rec["cwd"] = cwd
    rec.update(extra)
    return rec


_DISCOVER_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_discover_lists_untracked_transcripts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _DISCOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj", timestamp="2026-08-01T00:00:00Z"),
    ])
    rc = cli.main(["discover"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _DISCOVER_SID[:8] in out
    assert "/home/u/proj" in out
    assert "a prompt" in out


def test_discover_collapses_a_worktrees_fan_out_into_one_row(tmp_path, monkeypatch, capsys):
    # #34: two untracked transcripts in one .claude/worktrees checkout fold
    # into a single row (with a "+N more" note); a normal-cwd transcript
    # beside them stays its own row.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    wt = "/home/u/proj/.claude/worktrees/feat"
    wt_sids = ["aaaaaaaa-1111-4111-8111-111111111111",
               "bbbbbbbb-2222-4222-8222-222222222222"]
    for sid in wt_sids:
        _write_discover_transcript(tmp_path / "home", wt, sid,
                                   [_discover_user_rec("subagent run", cwd=wt)])
    normal_sid = "cccccccc-3333-4333-8333-333333333333"
    _write_discover_transcript(tmp_path / "home", "/home/u/proj/scripts", normal_sid,
                               [_discover_user_rec("a real conversation", cwd="/home/u/proj/scripts")])
    rc = cli.main(["discover"])
    out = capsys.readouterr().out
    assert rc == 0
    # The two worktree transcripts collapsed: exactly ONE representative sid
    # shows, and it announces the folded sibling.
    assert sum(sid[:8] in out for sid in wt_sids) == 1
    assert "(+1 more in this worktree)" in out
    # The non-worktree conversation is untouched.
    assert normal_sid[:8] in out


def test_discover_excludes_journaled_sessions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    entry = store.read(4242)
    entry["claude"] = _claude_field(_DISCOVER_SID)
    store.write(entry)
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _DISCOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])
    rc = cli.main(["discover"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _DISCOVER_SID[:8] not in out
    assert "no discoverable" in out


def test_discover_empty_prints_a_clean_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    rc = cli.main(["discover"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no discoverable (untracked) transcripts" in out


def test_discover_adopt_writes_a_valid_journal_entry(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _DISCOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])
    rc = cli.main(["discover", "--adopt", _DISCOVER_SID])
    out = capsys.readouterr().out
    assert rc == 0
    assert "adopted" in out
    assert _DISCOVER_SID[:8] in out
    # Discloses the competing-resume hazard, not just "no attach": an
    # adopted entry is always a revive candidate, so if the real session
    # is still running elsewhere the watchdog will start a second
    # `claude --resume` on it.
    assert "does NOT attach to a running process" in out
    assert "second" in out and "claude --resume" in out

    store = JournalStore(tmp_path / "state")
    scan = store.scan()
    assert not scan.problems
    matches = [e for e in scan.entries if e["claude"]["session_id"] == _DISCOVER_SID]
    assert len(matches) == 1
    entry = matches[0]
    contracts.validate_journal_entry(entry)
    assert entry["cwd"] == "/home/u/proj"
    assert entry["claude"]["sid_source"] == "guessed"
    assert entry["tmux_session"] is None


def test_discover_adopt_uses_the_transcripts_authoritative_cwd(tmp_path, monkeypatch, capsys):
    # The project dir name is "-home-u-Real-Dashed-Proj", which a naive
    # decode would mangle into "/home/u/Real/Dashed/Proj". The transcript's
    # own stamped cwd is what must land in the adopted entry.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/Real-Dashed-Proj", _DISCOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/Real-Dashed-Proj"),
    ])
    rc = cli.main(["discover", "--adopt", _DISCOVER_SID])
    assert rc == 0
    entry = JournalStore(tmp_path / "state").scan().entries[0]
    assert entry["cwd"] == "/home/u/Real-Dashed-Proj"


def test_discover_adopt_rejects_a_malformed_sid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    rc = cli.main(["discover", "--adopt", "not-a-uuid"])
    assert rc == 2
    assert "not a valid session id" in capsys.readouterr().err


def test_discover_adopt_reports_a_non_discoverable_sid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    rc = cli.main(["discover", "--adopt", _DISCOVER_SID])
    assert rc == 1
    assert "not a discoverable" in capsys.readouterr().err


def test_discover_adopt_is_idempotent_on_repeat(tmp_path, monkeypatch, capsys):
    # Re-adopting the same sid lands in the same pid slot rather than
    # leaking a second journal file.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _DISCOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])
    rc1 = cli.main(["discover", "--adopt", _DISCOVER_SID])
    assert rc1 == 0
    # After the first adopt the sid is tracked, so it's no longer
    # "discoverable" -> the second attempt refuses honestly rather than
    # duplicating the entry.
    capsys.readouterr()
    rc2 = cli.main(["discover", "--adopt", _DISCOVER_SID])
    assert rc2 == 1
    store = JournalStore(tmp_path / "state")
    matches = [e for e in store.scan().entries if e["claude"]["session_id"] == _DISCOVER_SID]
    assert len(matches) == 1


def test_discover_adopt_refuses_a_pid_slot_collision(tmp_path, monkeypatch, capsys):
    # A different session's journal entry already occupies the deterministic
    # synthetic pid slot discovery.adopted_pid(_DISCOVER_SID) would land in.
    # Adopt must refuse rather than silently overwrite it.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    other_sid = "99999999-9999-4999-8999-999999999999"
    colliding_pid = discovery.adopted_pid(_DISCOVER_SID)
    store.write(new_entry(
        pid=colliding_pid, cwd="/home/u/other", host="tmux", shell="zsh",
        boot_id="some-real-boot", now="2026-08-01T00:00:00Z",
        claude=_claude_field(other_sid),
    ))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _DISCOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])

    rc = cli.main(["discover", "--adopt", _DISCOVER_SID])
    err = capsys.readouterr().err
    assert rc == 1
    assert "collision" in err

    # The pre-existing entry must survive untouched.
    entry = store.read(colliding_pid)
    assert entry["claude"]["session_id"] == other_sid
    assert entry["cwd"] == "/home/u/other"


def test_discover_survives_a_naive_last_active_timestamp(tmp_path, monkeypatch, capsys):
    # fromisoformat happily parses a timestamp with no UTC offset; naively
    # subtracting it from the (always-aware) `_now()` raises TypeError, not
    # ValueError — that must not crash the whole listing.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _DISCOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj", timestamp="2026-08-01T00:00:00"),
    ])
    rc = cli.main(["discover"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _DISCOVER_SID[:8] in out


def test_discover_surfaces_corrupt_journal_files_on_stderr(tmp_path, monkeypatch, capsys):
    # Mirrors test_retrack_surfaces_scan_problems_on_stderr: a corrupt
    # tabs/<pid>.json must be reported, never silently dropped — dropping
    # it would leave that pid's sid out of the "journaled" exclusion set
    # and surface an already-tracked session as falsely "discoverable".
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    tabs_dir = tmp_path / "state" / "tabs"
    tabs_dir.mkdir(parents=True)
    (tabs_dir / "99.json").write_text("not json", encoding="utf-8")

    rc = cli.main(["discover"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "99.json" in err
    assert "no discoverable" in out


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

    def attached_sessions(self):
        return set()  # nothing opened yet -> the parked session is offered


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


class _FakeTmuxUnknown:
    """F16: available() but list_sessions() can't determine liveness."""

    def __init__(self, *a, **k):
        pass

    def available(self):
        return True

    def list_sessions(self):
        return None

    def attached_sessions(self):
        return None


def test_rescued_reports_none_when_tmux_liveness_is_unknown(tmp_path, monkeypatch, capsys):
    # F16: unknown liveness must never be silently treated as "definitely
    # rescued" — but per spec it degrades to the same "no rescued sessions"
    # a genuinely-empty tmux server produces (never a prompt on unknown).
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxUnknown)

    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    store = JournalStore(tmp_path)
    store.write(new_entry(  # would be rescued if liveness were confirmed
        pid=42, cwd=str(tmp_path), host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid), tmux_session="crr-8a1b2c3d",
    ))

    rc = cli.main(["rescued"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "no rescued sessions" in out
    # Finding 2 (re-audit): the None-liveness degrade used to be silent —
    # mirror the sibling journal-problems stderr pattern instead of
    # quietly undercounting.
    assert "crr rescued: tmux state unknown — rescued sessions may be undercounted" in err


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
    cli.rescue.claim_prompt(tmp_path, "current-boot")  # already prompted this boot
    calls = []
    monkeypatch.setattr(cli.ops, "reopen", lambda *a, **k: calls.append(a))

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


def test_rescue_check_silent_when_tmux_liveness_is_unknown(tmp_path, monkeypatch, capsys):
    # F16: unknown liveness must never surface a prompt (a false "rescued
    # session" the user can't actually attach to would be worse than
    # staying silent) — real rescue.rescued_sessions is exercised here
    # (not mocked) to prove the None->set() conversion happens before it,
    # since `sid in None` would otherwise raise.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxUnknown)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    store = JournalStore(tmp_path)
    store.write(new_entry(  # would be rescued if liveness were confirmed
        pid=42, cwd=str(tmp_path), host="tmux", shell="zsh", boot_id="old-boot",
        now="2026-07-24T00:00:00Z", claude=_claude_field(sid), tmux_session="crr-8a1b2c3d",
    ))

    rc = cli.main(["rescue-check"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == ""  # never a prompt on unconfirmed liveness
    # Finding 3 (re-audit): same stderr note as `crr rescued` (item 2), under
    # this command's own name. The interactive shims redirect stderr to
    # /dev/null on startup, so this stays quiet there; a manual
    # `crr rescue-check` sees it.
    assert "crr rescue-check: tmux state unknown — rescued sessions may be undercounted" in err
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is False  # nothing claimed


def test_rescue_check_headless_prompts_and_declines_once(tmp_path, monkeypatch, capsys):
    # Superseded by #headless: a genuinely headless host (tabs_expected
    # False) now PROMPTS like the GUI path instead of degrading straight to
    # a notice (that notice remains, but only for the "tab concept exists
    # but unavailable" branch — see test_rescue_check_prints_notice_when_
    # tab_unavailable below). This still proves the once-per-boot claim
    # applies on the headless path too.
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: ([], [], []))  # timeout -> decline

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 conversation(s) restored after the last reboot" in out
    assert "not now — 'crr rescued' lists them" in out
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True

    rc2 = cli.main(["rescue-check"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert out2 == ""  # once-per-boot: second call is silent


def test_rescue_check_headless_notice_claims_before_printing(tmp_path, monkeypatch, capsys):
    """The headless-notice outcome must ALSO claim before it becomes
    visible (brief: "decide deliberately where the claim happens for the
    notice outcome too... claim before printing it") — not just the
    interactive [Y/n] prompt. Force the claim to lose and assert the
    notice text never reaches stdout, discriminating this from an
    implementation that prints the notice first and claims/marks after."""
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))
    monkeypatch.setattr(cli.rescue, "claim_prompt", lambda *a, **k: False)

    rc = cli.main(["rescue-check"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "" and err == ""


def test_rescue_check_prints_notice_when_tab_unavailable(tmp_path, monkeypatch, capsys):
    # tabs_expected True (host HAS a concept of tabs, e.g. WSL) but the
    # spawner itself is unavailable right now (e.g. a dead interop handler)
    # -> the honest notice, never a prompt. This is the one remaining
    # notice-only outcome after #headless moved the tabs_expected-False
    # branch onto the prompt+tmux path.
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, True))

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 conversation(s) restored after the last reboot" in out
    assert "'crr rescued' lists them" in out
    assert "tmux attach -t <name>" in out
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_yes_reopens_tabs_keeping_them_tracked_and_marks(tmp_path, monkeypatch, capsys):
    # [Y] must REOPEN each restored conversation (attach a tab, keep it
    # tracked) — NOT detmux, which untracks. So the conversations stay on
    # the dashboard and survive the next reboot too (#30 / #33 principle).
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    calls = []

    def fake_reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                    pid, now, *, grace, remote_control, tab_spawner, tabs_expected,
                    crr_bin=None, tab_health=None):
        calls.append(pid)
        return SimpleNamespace(ok=True, degraded=False, message=f"reopened {pid} as crr-x")

    # If the old detmux path is still wired, this stays untouched and the
    # assertion on `calls` fails — the discriminator between the two ops.
    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)
    monkeypatch.setattr(cli.ops, "detmux",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must reopen, not detmux")))

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [42, 43]
    assert "reopened 42" in out and "reopened 43" in out
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_auto_open_skips_prompt_and_reopens(tmp_path, monkeypatch, capsys):
    """rescue_auto_open=true (the default) must open tabs for restored
    sessions WITHOUT prompting — select.select must never be called, and
    ops.reopen must be called for each found session."""
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    # No config.toml written — rescue_auto_open defaults to True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))
    monkeypatch.setattr(cli.select, "select",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("select.select must not be called when auto_open is true")))

    calls = []

    def fake_reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                    pid, now, *, grace, remote_control, tab_spawner, tabs_expected,
                    crr_bin=None, tab_health=None):
        calls.append(pid)
        return SimpleNamespace(ok=True, degraded=False, message=f"reopened {pid} as crr-x")

    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [42, 43]
    assert "reopened 42" in out and "reopened 43" in out
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_disables_tab_after_first_degraded(tmp_path, monkeypatch, capsys):
    """When the first open_tab fails (degraded), rescue-check must stop
    passing the spawner to remaining sessions — they get the 'attach with:
    tmux ...' fallback instead of N identical spawn failures."""
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}, {"pid": 44}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))

    spawners_seen = []

    def fake_reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                    pid, now, *, grace, remote_control, tab_spawner, tabs_expected,
                    crr_bin=None, tab_health=None):
        spawners_seen.append(tab_spawner)
        return SimpleNamespace(ok=True, degraded=True,
                               message=f"reopened {pid} (no tab)")

    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)

    rc = cli.main(["rescue-check"])
    assert rc == 0
    # First call gets the real spawner; remaining get None after degraded.
    assert spawners_seen[0] is not None
    assert all(s is None for s in spawners_seen[1:])


def test_rescue_check_yes_routes_failure_message_to_stdout(tmp_path, monkeypatch, capsys):
    """All three shims invoke `crr rescue-check 2>/dev/null`, so anything
    written to stderr from this consent path is thrown away — a user who
    just typed 'y' would never see a reopen failure. Both success and
    failure messages from the post-consent loop must land on stdout."""
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    def fake_reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                    pid, now, *, grace, remote_control, tab_spawner, tabs_expected,
                    crr_bin=None, tab_health=None):
        if pid == 42:
            return SimpleNamespace(ok=True, degraded=False, message=f"crr: #{pid} de-tmuxed")
        return SimpleNamespace(ok=False, degraded=False, message=f"crr: #{pid} de-tmux failed")

    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)

    rc = cli.main(["rescue-check"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "#42 de-tmuxed" in out
    assert "#43 de-tmux failed" in out
    assert err == ""


def test_rescue_check_enter_defaults_to_yes(tmp_path, monkeypatch, capsys):
    # The decided-and-recorded half of the yes/no split: a typed EMPTY
    # line (just pressing Enter) is yes -- distinct from a TIMEOUT, which
    # is always "not now" (see test_rescue_check_timeout_declines). An
    # implementation that only accepts a literal "y" would pass every
    # other test here but fail this one.
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "\n")  # Enter, no text

    calls = []

    def fake_reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                    pid, now, *, grace, remote_control, tab_spawner, tabs_expected,
                    crr_bin=None, tab_health=None):
        calls.append(pid)
        return SimpleNamespace(ok=True, degraded=False, message=f"crr: #{pid} de-tmuxed")

    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)

    rc = cli.main(["rescue-check"])
    assert rc == 0
    assert calls == [42]
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_eof_declines(tmp_path, monkeypatch, capsys):
    # stdin closed mid-read (readline returns "") is a decline, same as a
    # timeout -- not an accident of "" also meaning Enter, since a real
    # EOF never reaches the strip()/lower() step that "\n" does.
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "")  # EOF

    calls = []
    monkeypatch.setattr(cli.ops, "reopen", lambda *a, **k: calls.append(1))

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not now" in out
    assert calls == []
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_timeout_declines(tmp_path, monkeypatch, capsys):
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: ([], [], []))

    calls = []
    monkeypatch.setattr(cli.ops, "reopen", lambda *a, **k: calls.append(1))

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not now" in out
    assert "'crr rescued' lists them" in out
    assert calls == []
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_keyboard_interrupt_declines(tmp_path, monkeypatch, capsys):
    # Ctrl-C while waiting at the [Y/n] prompt must behave like a timeout
    # (decline), not propagate — a shim hook that lets a KeyboardInterrupt
    # escape breaks the "never break the shell" guarantee. The marker was
    # already written by claim_prompt BEFORE this prompt was printed (the
    # winner claims before prompting), so the once-per-boot invariant holds
    # even though the interrupt unwinds past the detmux/decline branch.
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))

    def _raise_keyboard_interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.select, "select", _raise_keyboard_interrupt)

    calls = []
    monkeypatch.setattr(cli.ops, "reopen", lambda *a, **k: calls.append(1))

    rc = cli.main(["rescue-check"])  # must not raise
    out = capsys.readouterr().out
    assert rc == 0
    assert "not now" in out
    assert "'crr rescued' lists them" in out
    assert calls == []
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_loser_of_race_is_silent(tmp_path, monkeypatch, capsys):
    """Task-3 review's two-shell race: this shell loses the atomic claim
    (a concurrent shell already claimed this boot's prompt) — it must
    print nothing and never call detmux, matching the marker-exists case
    but reached via claim_prompt returning False rather than a pre-check."""
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.rescue, "claim_prompt", lambda *a, **k: False)

    calls = []
    monkeypatch.setattr(cli.ops, "reopen", lambda *a, **k: calls.append(1))

    rc = cli.main(["rescue-check"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "" and err == ""
    assert calls == []


def test_rescue_check_claims_before_prompt_survives_prompt_crash(tmp_path, monkeypatch, capsys):
    """Winner claims BEFORE printing/waiting on the [Y/n] prompt: pin this
    ordering by blowing up the prompt machinery (select.select) AFTER a
    successful claim — the marker must still be durable (no re-arming the
    prompt for the next shell) and the blanket exception guard in
    _cmd_rescue_check must still return rc 0."""
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.select, "select", _boom)

    rc = cli.main(["rescue-check"])
    assert rc == 0
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True


def test_rescue_check_auto_open_headless_skips_prompt(tmp_path, monkeypatch, capsys):
    """rescue_auto_open=true on the headless path: tmux-link fires without
    prompting.  select.select must never be called."""
    _rescue_check_setup(monkeypatch, tmp_path, [
        {"pid": 42, "tmux_session": "crr-a", "cwd": "/home/u/alpha"},
    ])
    # No config.toml — rescue_auto_open defaults to True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))  # headless
    monkeypatch.setenv("TMUX", "sock,1,0")  # inside tmux
    monkeypatch.setattr(cli.select, "select",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("select.select must not be called when auto_open is true")))

    ran = []
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli, "_exec", lambda *a: (_ for _ in ()).throw(
        AssertionError("must not exec when inside tmux")))

    class _T(_FakeTmuxRescued):
        def current_session_name(self): return "work"
    monkeypatch.setattr(cli.tmux, "RealTmux", lambda *a, **k: _T())

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert ["tmux", "link-window", "-s", "crr-a", "-t", "work"] in ran


def test_rescue_check_headless_in_tmux_links_windows_no_exec(tmp_path, monkeypatch, capsys):
    # Headless (tabs_expected False) + inside tmux + [Y] -> link each restored
    # session into the current tmux session; never exec.
    _rescue_check_setup(monkeypatch, tmp_path, [
        {"pid": 42, "tmux_session": "crr-a", "cwd": "/home/u/alpha"},
        {"pid": 43, "tmux_session": "crr-b", "cwd": "/home/u/beta"},
    ])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))  # headless
    monkeypatch.setenv("TMUX", "sock,1,0")  # inside tmux
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    ran = []
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli, "_exec", lambda *a: (_ for _ in ()).throw(
        AssertionError("must not exec when inside tmux")))

    class _T(_FakeTmuxRescued):
        def current_session_name(self): return "work"
    monkeypatch.setattr(cli.tmux, "RealTmux", lambda *a, **k: _T())

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert ["tmux", "link-window", "-s", "crr-a", "-t", "work"] in ran
    assert ["tmux", "link-window", "-s", "crr-b", "-t", "work"] in ran
    assert "Ctrl-b w" in out


def test_rescue_check_headless_not_in_tmux_execs_attach(tmp_path, monkeypatch, capsys):
    # Headless + NOT in tmux + [Y], single restored -> exec `tmux attach`.
    _rescue_check_setup(monkeypatch, tmp_path, [
        {"pid": 42, "tmux_session": "crr-a", "cwd": "/home/u/alpha"},
    ])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))
    monkeypatch.delenv("TMUX", raising=False)  # not in tmux
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)

    execed = []
    monkeypatch.setattr(cli, "_exec", lambda file, argv: execed.append((file, argv)))

    rc = cli.main(["rescue-check"])
    assert rc == 0
    assert execed == [("tmux", ["tmux", "attach", "-t", "crr-a"])]


def test_rescue_check_headless_decline_does_nothing(tmp_path, monkeypatch, capsys):
    _rescue_check_setup(monkeypatch, tmp_path, [
        {"pid": 42, "tmux_session": "crr-a", "cwd": "/home/u/alpha"},
    ])
    (tmp_path / "config.toml").write_text("rescue_auto_open = false\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "n\n")
    monkeypatch.setattr(cli, "_exec", lambda *a: (_ for _ in ()).throw(
        AssertionError("decline must not exec")))
    monkeypatch.setattr(cli.tmux, "RealTmux", _FakeTmuxRescued)

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not now" in out


class _FakeTmuxRevive:
    """Tracks new_detached_session calls; live set grows as sessions are created."""
    def __init__(self, *a, **k):
        self._live = set()
        self.created = []

    def available(self):
        return True

    def list_sessions(self):
        return set(self._live)

    def attached_sessions(self):
        return set()

    def new_detached_session(self, name, cwd, argv):
        self._live.add(name)
        self.created.append((name, cwd, argv))

    def session_pid(self, name):
        return None


def test_rescue_check_revives_archived_sessions_before_scanning(tmp_path, monkeypatch, capsys):
    """#100: shell startup must trigger a revive pass so archived sessions
    (superseded-on-register after reboot) get tmux sessions even without
    the watchdog running. The revived entry must then be re-journaled so
    rescued_sessions() finds it and the rescue prompt/reopen fires."""
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    fake_tmux = _FakeTmuxRevive()
    monkeypatch.setattr(cli.tmux, "RealTmux", lambda *a, **k: fake_tmux)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        lambda t: type("P", (), {
                            "is_alive": lambda s, pid: False,
                            "has_controlling_tty": lambda s, pid: False,
                            "controlling_ttys": lambda s, pids: set(),
                        })())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="old-boot-pre-reboot", now="2026-07-23T00:00:00Z",
        claude=_claude_field(sid),
    )
    archive.archive(entry, "superseded-on-register", "2026-07-24T00:00:00Z")

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))

    reopen_calls = []

    def fake_reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                    pid, now, *, grace, remote_control, tab_spawner, tabs_expected,
                    crr_bin=None, tab_health=None):
        reopen_calls.append(pid)
        return SimpleNamespace(ok=True, degraded=False, message=f"reopened {pid}")

    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)
    monkeypatch.setattr(cli.process_probe, "PsProcessController",
                        lambda t: type("C", (), {})())

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert fake_tmux.created, "archived session was not revived at shell startup"
    assert sid in fake_tmux.created[0][0]
    assert reopen_calls == [99], "revived archive entry must be offered via reopen"
    assert "reopened 99" in out


def test_rescue_check_fires_after_deregister_clears_markers(tmp_path, monkeypatch, capsys):
    """End-to-end: a claude-bearing shell exits (deregister), clearing rescue
    markers. The next shell startup (rescue-check) re-scans, revives the
    archived session, and offers it to the user. This is the chain that was
    broken before the fix: deregister archived the entry but left markers
    intact, so the next rescue-check short-circuited at already_prompted."""
    from crr.core import rescue

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())

    # 1. Seed a claude-bearing journal entry and mark "already prompted".
    store = JournalStore(tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _seed(store, 4242)
    entry = store.read(4242)
    entry["claude"] = _claude_field(sid)
    store.write(entry)
    rescue.claim_prompt(tmp_path, "current-boot")
    rescue.mark_revived(tmp_path, "current-boot")

    # 2. Shell exits — deregister archives + clears markers.
    assert cli.main(["deregister", "--pid", "4242"]) == 0
    assert not rescue.already_prompted(tmp_path, "current-boot")

    # 3. Next shell starts — rescue-check should revive + offer the session.
    fake_tmux = _FakeTmuxRevive()
    monkeypatch.setattr(cli.tmux, "RealTmux", lambda *a, **k: fake_tmux)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        lambda t: type("P", (), {
                            "is_alive": lambda s, pid: False,
                            "has_controlling_tty": lambda s, pid: False,
                            "controlling_ttys": lambda s, pids: set(),
                        })())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (_FakeTab(), True))
    monkeypatch.setattr(cli.process_probe, "PsProcessController",
                        lambda t: type("C", (), {})())

    reopen_calls = []

    def fake_reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                    pid, now, *, grace, remote_control, tab_spawner, tabs_expected,
                    crr_bin=None, tab_health=None):
        reopen_calls.append(pid)
        return SimpleNamespace(ok=True, degraded=False, message=f"reopened {pid}")

    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert fake_tmux.created, "archived session was not revived"
    assert reopen_calls, "rescue prompt must fire after deregister clears markers"
    assert "reopened" in out


def test_rescue_check_revive_pass_runs_at_most_once_per_boot(tmp_path, monkeypatch, capsys):
    """The revive pass must run once per boot, not every shell start.
    Without a dedicated marker, archive-only entries (no journal rescued
    sessions) never trigger claim_prompt, so subsequent shells re-sweep."""
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.boot_identity, "detect", lambda: _FakeBoot())
    fake_tmux = _FakeTmuxRevive()
    monkeypatch.setattr(cli.tmux, "RealTmux", lambda *a, **k: fake_tmux)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        lambda t: type("P", (), {
                            "is_alive": lambda s, pid: False,
                            "has_controlling_tty": lambda s, pid: False,
                            "controlling_ttys": lambda s, pids: set(),
                        })())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/home/u/proj", host="tmux", shell="zsh",
        boot_id="old-boot-pre-reboot", now="2026-07-23T00:00:00Z",
        claude=_claude_field(sid),
    )
    archive.archive(entry, "superseded-on-register", "2026-07-24T00:00:00Z")

    # Stub the os.execvp seam. This test drives the real rescue-check reopen
    # path twice; on a runner where that path reaches _exec, an unstubbed
    # seam replaces the pytest process itself (silent hard-kill, no traceback)
    # rather than failing. Every sibling rescue-check test stubs it likewise.
    monkeypatch.setattr(cli, "_exec", lambda *a, **k: None)

    revive_calls = []
    orig = cli.reviver.revive_crashed

    def spy(*a, **kw):
        revive_calls.append(1)
        return orig(*a, **kw)

    monkeypatch.setattr(cli.reviver, "revive_crashed", spy)

    cli.main(["rescue-check"])
    assert len(revive_calls) == 1, "first rescue-check must run the revive pass"

    cli.main(["rescue-check"])
    assert len(revive_calls) == 1, "second rescue-check must NOT re-run the revive pass"


def test_exec_seam_is_forbidden_unless_a_test_stubs_it():
    """The conftest autouse guard defaults cli._exec to a raise, so a test that
    reaches the exec seam without stubbing fails loudly instead of replacing
    the pytest process. Regression guard for the Ubuntu-CI hard-kill; this test
    deliberately does NOT stub _exec, to assert the default is protective."""
    with pytest.raises(RuntimeError, match="os.execvp"):
        cli._exec("echo", ["echo", "hi"])


def test_repair_check_prints_relaunch_kind_and_sid(tmp_path, monkeypatch, capsys):
    from crr.core.flags import FlagStore
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    FlagStore(tmp_path).arm_relaunch(4242, "sid-xyz", boot_id="b")
    rc = cli.main(["repair-check", "--pid", "4242"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "relaunch sid-xyz"


def test_repair_check_prints_close_kind(tmp_path, monkeypatch, capsys):
    from crr.core.flags import FlagStore
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    FlagStore(tmp_path).arm_close(4242, boot_id="b")
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
    flags.arm_close(4242, boot_id="b")
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
        claude={"session_id": sid, "sid_source": "injected", "started": "2026-07-24T00:00:00Z",
                "skip_permissions": False},
    ))
    try:
        rc = cli.main(["revive"])
        assert rc == 0
        # [#58] The entry is re-keyed onto the pid actually running in the
        # pane — the revived claude never runs the shim, so this is the only
        # way the live conversation gets a card at all. The seeded shell pid
        # is gone precisely because the conversation moved off it.
        with pytest.raises(KeyError):
            store.read(4242)
        entry = next(e for e in store.scan().entries
                     if (e.get("claude") or {}).get("session_id") == sid)
        assert entry["pid"] != 4242
        assert entry["tmux_session"] == f"crr-{sid}"
        assert entry["revive_strikes"] == 1
        sessions = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        ).stdout
        assert f"crr-{sid}" in sessions
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


def test_shim_output_carries_a_version_stamp(capsys):
    """[audit P7] generated shims stamp the crr + config-defaults versions."""
    assert cli.main(["shim", "bash", "--crr-bin", "/x/crr"]) == 0
    out = capsys.readouterr().out
    assert "generated by crr " in out and "config-defaults v" in out
    assert "@CRR_VERSION@" not in out and "@CRR_DEFAULTS_V@" not in out


# --- recall (F1 — print-only transcript search) ---------------------------


def _write_recall_transcript(home, cwd, sid, records):
    d = home / ".claude" / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _user_rec(text, **extra):
    r = {"type": "user", "message": {"role": "user", "content": text}}
    r.update(extra)
    return r


def _assistant_rec(text, model=None):
    msg = {"role": "assistant", "content": text}
    if model is not None:
        msg["model"] = model
    return {"type": "assistant", "message": msg}


_RECALL_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_recall_pid_resolves_sid_and_prints_matches(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    entry = store.read(4242)
    entry["claude"] = _claude_field(_RECALL_SID)
    store.write(entry)
    _write_recall_transcript(tmp_path / "home", "/home/u/proj", _RECALL_SID, [
        _user_rec("what did we decide about the fox migration?", timestamp="2026-01-01T00:00:00Z"),
        _assistant_rec("we agreed to ship the fox migration Friday"),
    ])
    rc = cli.main(["recall", "--pid", "4242", "fox"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fox migration" in out
    assert "fox migration Friday" in out


def test_recall_sid_used_directly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_recall_transcript(tmp_path / "home", "/home/u/proj", _RECALL_SID, [
        _user_rec("a fox prompt"),
    ])
    rc = cli.main(["recall", "--sid", _RECALL_SID, "fox"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "a fox prompt" in out


def test_recall_no_matches_prints_clean_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_recall_transcript(tmp_path / "home", "/home/u/proj", _RECALL_SID, [
        _user_rec("hello there"),
    ])
    rc = cli.main(["recall", "--sid", _RECALL_SID, "nonexistent"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no matches for 'nonexistent'" in out


def test_recall_dash_n_caps_the_printed_matches(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    records = [
        _user_rec(f"fox message {i}", timestamp=f"2026-01-0{i}T00:00:00Z")
        for i in range(1, 6)  # 5 matches
    ]
    _write_recall_transcript(tmp_path / "home", "/home/u/proj", _RECALL_SID, records)
    rc = cli.main(["recall", "--sid", _RECALL_SID, "-n", "2", "fox"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 2
    # most-recent-first: message 5 then message 4
    assert "fox message 5" in lines[0]
    assert "fox message 4" in lines[1]


def test_recall_all_searches_every_transcript_in_the_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    sid_a = "aaaaaaaa-1111-4111-8111-111111111111"
    sid_b = "bbbbbbbb-2222-4222-8222-222222222222"
    _write_recall_transcript(tmp_path / "home", "/home/u/proj", sid_a, [
        _user_rec("fox in session A", timestamp="2026-01-01T00:00:00Z"),
    ])
    _write_recall_transcript(tmp_path / "home", "/home/u/proj", sid_b, [
        _user_rec("fox in session B", timestamp="2026-01-02T00:00:00Z"),
    ])
    rc = cli.main(["recall", "--all", "--cwd", "/home/u/proj", "fox"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fox in session A" in out
    assert "fox in session B" in out


def test_recall_all_derives_cwd_from_pid_entry(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")  # no claude session on this pid
    _write_recall_transcript(tmp_path / "home", "/home/u/proj", _RECALL_SID, [
        _user_rec("fox from derived cwd"),
    ])
    rc = cli.main(["recall", "--pid", "4242", "--all", "fox"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fox from derived cwd" in out


def test_recall_requires_pid_sid_or_all(capsys):
    rc = cli.main(["recall", "fox"])
    assert rc == 2
    assert "specify --pid, --sid, or --all" in capsys.readouterr().err


def test_recall_rejects_both_pid_and_sid(capsys):
    rc = cli.main(["recall", "--pid", "1", "--sid", _RECALL_SID, "fox"])
    assert rc == 2
    assert "only one of --pid / --sid" in capsys.readouterr().err


def test_recall_rejects_sid_combined_with_all(capsys):
    # --sid names one transcript; --all means "every transcript in the cwd".
    # Combined, --sid was previously silently ignored and the scope quietly
    # widened to --all — reject instead of widening scope behind the user's
    # back.
    rc = cli.main(["recall", "--sid", _RECALL_SID, "--all", "--cwd", "/home/u/proj", "fox"])
    assert rc == 2
    assert "--sid" in capsys.readouterr().err


def test_recall_unknown_pid_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    rc = cli.main(["recall", "--pid", "9999", "fox"])
    assert rc == 2
    assert "no journal entry for pid 9999" in capsys.readouterr().err


def test_recall_pid_without_claude_session_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")  # claude is None
    rc = cli.main(["recall", "--pid", "4242", "fox"])
    assert rc == 2
    assert "no claude session" in capsys.readouterr().err


def test_recall_all_without_cwd_or_pid_errors(capsys):
    rc = cli.main(["recall", "--all", "fox"])
    assert rc == 2
    assert "--all needs --cwd or --pid" in capsys.readouterr().err


def test_recall_rejects_an_empty_query(capsys):
    # An empty (or whitespace-only) query would match every real turn in
    # the transcript ("" is a substring of everything) — reject it with a
    # clear error instead of silently widening the search to "everything".
    rc = cli.main(["recall", "--sid", _RECALL_SID, ""])
    assert rc == 2
    assert "query" in capsys.readouterr().err


def test_recall_rejects_a_whitespace_only_query(capsys):
    rc = cli.main(["recall", "--sid", _RECALL_SID, "   "])
    assert rc == 2
    assert "query" in capsys.readouterr().err


def test_recall_rejects_a_junk_sid(tmp_path, monkeypatch, capsys):
    # Mirrors claude-launch's guard on a user-typed --session-id: an
    # arbitrary string must not reach find_transcript's glob unvalidated.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    rc = cli.main(["recall", "--sid", "../../etc/passwd", "fox"])
    assert rc == 2
    assert "not a valid session id" in capsys.readouterr().err


def test_recall_never_re_injects_only_prints(tmp_path, monkeypatch, capsys):
    # Retrieval-only: crr recall must never touch the journal or spawn
    # anything — it's a read of the transcript on disk, nothing else.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    store = JournalStore(tmp_path / "state")
    _seed(store, 4242, cwd="/home/u/proj")
    entry = store.read(4242)
    entry["claude"] = _claude_field(_RECALL_SID)
    store.write(entry)
    before = store.read(4242)
    _write_recall_transcript(tmp_path / "home", "/home/u/proj", _RECALL_SID, [
        _user_rec("a fox prompt"),
    ])
    cli.main(["recall", "--pid", "4242", "fox"])
    assert store.read(4242) == before


# --- adopt --takeover (safe adoption of a still-live session) --------------

_TAKEOVER_SID = "7b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"


class _FakeResumeController:
    """Records find_resume_process/terminate_group calls into a SHARED
    ``calls`` list (with _FakeTakeoverFlags below) so cross-object call
    ORDER can be asserted, not just each object's own call count.

    ``proc`` is either a single ResumeProcess/None (returned on every call —
    the common case) or a LIST, which is call-scripted: each call pops the
    next value until one remains, then that last value repeats forever
    (same repeat-last-value convention as the module-level ``_scripted``
    helper) — lets a test give ``find_resume_process`` a different answer
    on the top-of-function resolve vs. the under-lock re-resolve.
    """

    def __init__(self, proc, calls, raise_on_terminate=None):
        self._proc = proc
        self._calls = calls
        self._raise = raise_on_terminate

    def find_resume_process(self, session_id):
        self._calls.append(("find_resume_process", session_id))
        if isinstance(self._proc, list):
            return self._proc.pop(0) if len(self._proc) > 1 else self._proc[0]
        return self._proc

    def terminate_group(self, pgid, grace_seconds):
        self._calls.append(("terminate_group", pgid, grace_seconds))
        if self._raise is not None:
            raise self._raise


class _FakeTakeoverFlags:
    def __init__(self, calls):
        self._calls = calls
        self.armed: set[int] = set()

    def arm_close(self, pid, *, boot_id):
        self._calls.append(("arm_close", pid))
        self.armed.add(pid)

    def clear(self, pid):
        self._calls.append(("clear", pid))
        self.armed.discard(pid)


class _RecordingStore(JournalStore):
    """A JournalStore that appends a marker to the shared ``calls`` list on
    every write — lets a test see exactly where, relative to arm_close /
    terminate_group, the adoption's journal write lands."""

    def __init__(self, sd, calls):
        super().__init__(sd)
        self._calls = calls

    def write(self, entry):
        claude = entry.get("claude")
        self._calls.append(("store.write", claude["session_id"] if claude else None))
        super().write(entry)


def _scripted(values):
    """A zero/positional-arg callable yielding ``values`` in order, then
    repeating the last value forever (a test scripts exactly the values
    that matter and needn't predict every remaining call)."""
    values = list(values)

    def fn(*_a, **_kw):
        return values.pop(0) if len(values) > 1 else values[0]

    return fn


def _failing_sleep(seconds):
    raise AssertionError(f"unexpected sleep({seconds}) — refuse-fast must not wait")


def test_takeover_happy_path_orders_arm_before_kill_before_adopt(tmp_path, monkeypatch):
    # Two DIFFERENT ResumeProcess tuples: the first is the top-of-function
    # resolve (only confirms a target exists / gates the wait), the second
    # is the under-the-lock re-resolve that the kill must actually use. If
    # the kill ever reused the stale first tuple, this test would see
    # arm_close(50) and terminate_group(100, ...) instead of the fresh
    # ppid/pgid — and "stopped live pid 100" instead of 101.
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _TAKEOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])
    calls: list = []
    store = _RecordingStore(tmp_path / "state", calls)
    proc1 = ResumeProcess(pid=100, ppid=50, pgid=100)
    proc2 = ResumeProcess(pid=101, ppid=51, pgid=777)
    controller = _FakeResumeController([proc1, proc2], calls)
    flags = _FakeTakeoverFlags(calls)
    config = cfg.Config()
    read_signal = lambda sid: {"mtime": 100.0, "tail_kind": "assistant-end"}
    clock = _scripted([500.0, 1000.0])  # deadline calc, then seconds_idle calc

    ok, msg = cli._takeover(
        store, tmp_path / "state", config, controller, flags, _TAKEOVER_SID,
        max_wait=180.0, read_signal=read_signal, clock=clock, sleep=_failing_sleep, boot_id="test-boot",
    )
    assert ok
    assert msg.startswith("took over ")
    assert _TAKEOVER_SID[:8] in msg
    assert "stopped live pid 101" in msg

    # Both resolves happened (top-of-function, then the under-lock re-resolve).
    assert [c for c in calls if c[0] == "find_resume_process"] == [
        ("find_resume_process", _TAKEOVER_SID),
        ("find_resume_process", _TAKEOVER_SID),
    ]
    # arm_close(ppid) strictly before terminate_group(pgid, grace); the
    # journal write (adoption) strictly after the kill — and both use the
    # SECOND (fresh) tuple, never the first (stale) one.
    kinds = [c[0] for c in calls]
    assert kinds.index("arm_close") < kinds.index("terminate_group")
    assert kinds.index("terminate_group") < kinds.index("store.write")
    assert ("arm_close", 51) in calls
    assert ("arm_close", 50) not in calls
    assert ("terminate_group", 777, config.get("close_grace_seconds")) in calls
    assert not any(c[0] == "terminate_group" and c[1] == 100 for c in calls)

    scan = store.scan()
    matches = [e for e in scan.entries if e.get("claude", {}).get("session_id") == _TAKEOVER_SID]
    assert len(matches) == 1


def test_takeover_success_message_omits_the_competing_session_warning(tmp_path, monkeypatch):
    # Plain adopt warns "if the session is still alive elsewhere, the watchdog
    # will start a SECOND claude --resume" — takeover just STOPPED the live
    # process, so that warning is false here and must be dropped.
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _TAKEOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])
    calls: list = []
    store = _RecordingStore(tmp_path / "state", calls)
    proc = ResumeProcess(pid=101, ppid=51, pgid=777)
    controller = _FakeResumeController([proc, proc], calls)
    flags = _FakeTakeoverFlags(calls)
    ok, msg = cli._takeover(
        store, tmp_path / "state", cfg.Config(), controller, flags, _TAKEOVER_SID,
        max_wait=180.0, boot_id="test-boot",
        read_signal=lambda sid: {"mtime": 100.0, "tail_kind": "assistant-end"},
        clock=_scripted([500.0, 1000.0]), sleep=_failing_sleep,
    )
    assert ok
    assert "still alive elsewhere" not in msg
    assert "second `claude --resume`" not in msg
    # still an honest adoption message
    assert "adopted" in msg and "recoverable" in msg


def test_plain_adopt_keeps_the_competing_session_warning(tmp_path, monkeypatch):
    # The default path (crr discover --adopt / crr adopt, no takeover) has NOT
    # stopped any live process, so it must keep disclosing the hazard.
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _TAKEOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])
    store = JournalStore(tmp_path / "state")
    ok, msg = cli._adopt(store, tmp_path / "state", _TAKEOVER_SID)
    assert ok
    assert "still alive elsewhere" in msg


def test_web_takeover_refuses_when_no_live_process(tmp_path, monkeypatch):
    # The dashboard takeover path (max_wait=0.0) still refuses honestly when
    # nothing is running for the sid — no kill, no flag.
    set_home(monkeypatch, str(tmp_path / "home"))
    calls: list = []
    store = JournalStore(tmp_path / "state")
    controller = _FakeResumeController([None], calls)
    flags = _FakeTakeoverFlags(calls)
    ok, msg = cli._web_takeover(
        store, tmp_path / "state", cfg.Config(), controller, flags, _TAKEOVER_SID, boot_id="test-boot",
        read_signal=lambda sid: {"mtime": 0.0, "tail_kind": ""},
        clock=_scripted([0.0]), sleep=_failing_sleep,
    )
    assert not ok
    assert "no live" in msg
    assert not any(c[0] == "terminate_group" for c in calls)


def test_web_takeover_translates_the_mid_turn_refusal_for_the_phone(tmp_path, monkeypatch):
    # max_wait=0.0 makes a busy session refuse with the internal "still
    # actively writing after 0s" — a phone user must see something sane.
    set_home(monkeypatch, str(tmp_path / "home"))
    calls: list = []
    store = JournalStore(tmp_path / "state")
    proc = ResumeProcess(pid=101, ppid=51, pgid=777)
    controller = _FakeResumeController([proc, proc], calls)
    flags = _FakeTakeoverFlags(calls)
    ok, msg = cli._web_takeover(
        store, tmp_path / "state", cfg.Config(), controller, flags, _TAKEOVER_SID, boot_id="test-boot",
        read_signal=lambda sid: {"mtime": 1000.0, "tail_kind": "mid-turn"},
        clock=_scripted([1000.0]), sleep=_failing_sleep,
    )
    assert not ok
    assert "mid-turn" in msg
    assert "0s" not in msg  # the raw "after 0s" wording is gone
    assert not any(c[0] == "terminate_group" for c in calls)


def test_takeover_re_resolves_process_under_lock_before_kill(tmp_path, monkeypatch):
    # find_resume_process returns a valid process on the FIRST call
    # (top-of-function resolve) but None on the SECOND call — the
    # under-lock re-resolve immediately before the kill. This models the
    # live process exiting (and its pid/ppid/pgid being recycled) during
    # the lock-free wait loop. Must refuse honestly using the FRESH (None)
    # result: no kill, no flag armed on the stale first-call tuple.
    set_home(monkeypatch, str(tmp_path / "home"))
    calls: list = []
    store = JournalStore(tmp_path / "state")
    proc1 = ResumeProcess(pid=100, ppid=50, pgid=100)
    controller = _FakeResumeController([proc1, None], calls)
    flags = _FakeTakeoverFlags(calls)
    config = cfg.Config()

    ok, msg = cli._takeover(
        store, tmp_path / "state", config, controller, flags, _TAKEOVER_SID,
        max_wait=180.0, boot_id="test-boot",
        read_signal=lambda sid: {"mtime": 100.0, "tail_kind": "assistant-end"},
        clock=_scripted([500.0, 1000.0]), sleep=_failing_sleep,
    )
    assert not ok
    assert _TAKEOVER_SID[:8] in msg
    assert "exited" in msg
    assert "adopt without --takeover" in msg
    assert not any(c[0] == "terminate_group" for c in calls)
    assert flags.armed == set()
    assert [c for c in calls if c[0] == "find_resume_process"] == [
        ("find_resume_process", _TAKEOVER_SID),
        ("find_resume_process", _TAKEOVER_SID),
    ]


def test_takeover_polls_through_activity_then_takes_over(tmp_path, monkeypatch):
    # Spec bullet "cli wait loop timing": a session that STREAMS (busy) for
    # several polls, then stops at assistant-end, must become ready without
    # ever hitting max_wait — the only path exercising the loop's
    # cross-iteration seconds_idle recompute and sleep(poll) cadence. mtime
    # advances with the clock while busy (seconds_idle stays a small
    # constant, not a huge negative one — a real streaming transcript), then
    # freezes while the clock jumps forward past the idle window.
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _TAKEOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])
    calls: list = []
    store = _RecordingStore(tmp_path / "state", calls)
    proc = ResumeProcess(pid=100, ppid=50, pgid=100)
    controller = _FakeResumeController(proc, calls)
    flags = _FakeTakeoverFlags(calls)
    config = cfg.Config()
    assert config.get("takeover_idle_seconds") == 20.0  # this test's math assumes it
    sleeps: list = []

    read_signal = _scripted([
        {"mtime": 1001.0, "tail_kind": "mid-turn"},   # busy poll 1
        {"mtime": 1003.0, "tail_kind": "mid-turn"},   # busy poll 2
        {"mtime": 1005.0, "tail_kind": "mid-turn"},   # busy poll 3
        {"mtime": 1005.0, "tail_kind": "assistant-end"},  # writing stopped
    ])
    clock = _scripted([
        1000.0,  # deadline calc
        1002.0, 1002.0,  # poll 1: seconds_idle, then deadline check
        1004.0, 1004.0,  # poll 2: seconds_idle, then deadline check
        1006.0, 1006.0,  # poll 3: seconds_idle, then deadline check
        1030.0,          # poll 4: seconds_idle = 25 >= idle_window(20) -> ready
    ])

    ok, msg = cli._takeover(
        store, tmp_path / "state", config, controller, flags, _TAKEOVER_SID,
        max_wait=100.0, read_signal=read_signal, clock=clock, boot_id="test-boot",
        sleep=lambda s: sleeps.append(s),
    )
    assert ok
    assert msg.startswith("took over ")
    assert "still actively writing" not in msg
    assert len(sleeps) == 3
    assert sleeps == [config.get("takeover_poll_seconds")] * 3
    assert ("terminate_group", 100, config.get("close_grace_seconds")) in calls


def test_takeover_no_live_process_refuses_without_kill_or_flag(tmp_path):
    calls: list = []
    store = JournalStore(tmp_path)
    controller = _FakeResumeController(None, calls)
    flags = _FakeTakeoverFlags(calls)
    config = cfg.Config()

    ok, msg = cli._takeover(
        store, tmp_path, config, controller, flags, _TAKEOVER_SID,
        max_wait=180.0, read_signal=lambda sid: {"mtime": 0.0, "tail_kind": ""}, boot_id="test-boot",
        clock=_scripted([0.0]), sleep=_failing_sleep,
    )
    assert not ok
    assert f"claude --resume {_TAKEOVER_SID}" in msg
    assert "no live" in msg
    assert not any(c[0] == "terminate_group" for c in calls)
    assert flags.armed == set()


def test_takeover_refuses_fast_when_idle_but_parked_mid_turn(tmp_path):
    # First poll is already quiet (seconds_idle >= idle_window) but the
    # tail is NOT a clean boundary -> must refuse IMMEDIATELY, never
    # sleeping out the (very long) max_wait deadline.
    calls: list = []
    store = JournalStore(tmp_path)
    proc = ResumeProcess(pid=100, ppid=50, pgid=100)
    controller = _FakeResumeController(proc, calls)
    flags = _FakeTakeoverFlags(calls)
    config = cfg.Config()

    ok, msg = cli._takeover(
        store, tmp_path, config, controller, flags, _TAKEOVER_SID,
        max_wait=100_000.0, boot_id="test-boot",
        read_signal=lambda sid: {"mtime": 0.0, "tail_kind": "mid-turn"},
        clock=_scripted([1000.0, 1000.0]), sleep=_failing_sleep,
    )
    assert not ok
    assert "idle but parked at mid-turn" in msg
    assert not any(c[0] == "terminate_group" for c in calls)
    assert flags.armed == set()


def test_takeover_times_out_while_still_actively_writing(tmp_path):
    calls: list = []
    store = JournalStore(tmp_path)
    proc = ResumeProcess(pid=100, ppid=50, pgid=100)
    controller = _FakeResumeController(proc, calls)
    flags = _FakeTakeoverFlags(calls)
    config = cfg.Config()
    sleeps: list = []

    # mtime pinned far in the "future" relative to the scripted clock, so
    # seconds_idle is always deeply negative (< idle_window) -> "busy"
    # every iteration, until clock() crosses the max_wait deadline.
    ok, msg = cli._takeover(
        store, tmp_path, config, controller, flags, _TAKEOVER_SID,
        max_wait=5.0, boot_id="test-boot",
        read_signal=lambda sid: {"mtime": 1_000_000.0, "tail_kind": "mid-turn"},
        clock=_scripted([0.0, 1.0, 1.0, 3.0, 3.0, 6.0, 6.0]),
        sleep=lambda s: sleeps.append(s),
    )
    assert not ok
    assert "still actively writing after 5s" in msg
    assert not any(c[0] == "terminate_group" for c in calls)
    assert flags.armed == set()
    assert len(sleeps) == 2  # polled twice before the deadline tripped


def test_takeover_refuses_when_sid_becomes_tracked_before_the_kill(tmp_path):
    # Ready to take over, but the exclusion re-check finds the sid already
    # journaled (a resolve->kill race) -> refuse, no kill, no flag.
    calls: list = []
    store = JournalStore(tmp_path)
    _seed(store, 4242, cwd="/home/u/proj")
    entry = store.read(4242)
    entry["claude"] = _claude_field(_TAKEOVER_SID)
    store.write(entry)

    proc = ResumeProcess(pid=100, ppid=50, pgid=100)
    controller = _FakeResumeController(proc, calls)
    flags = _FakeTakeoverFlags(calls)
    config = cfg.Config()

    ok, msg = cli._takeover(
        store, tmp_path, config, controller, flags, _TAKEOVER_SID,
        max_wait=180.0, boot_id="test-boot",
        read_signal=lambda sid: {"mtime": 100.0, "tail_kind": "assistant-end"},
        clock=_scripted([500.0, 1000.0]), sleep=_failing_sleep,
    )
    assert not ok
    assert f"{_TAKEOVER_SID[:8]} is now tracked" in msg
    # The exclusion re-check must happen BEFORE arm_close (not merely
    # "armed-then-rolled-back") — assert the flag call never happens at all.
    assert not any(c[0] == "arm_close" for c in calls)
    assert not any(c[0] == "terminate_group" for c in calls)
    assert flags.armed == set()


def test_takeover_rolls_back_the_flag_when_the_kill_fails(tmp_path):
    calls: list = []
    store = JournalStore(tmp_path)
    proc = ResumeProcess(pid=100, ppid=50, pgid=100)
    controller = _FakeResumeController(proc, calls, raise_on_terminate=OSError("no such process"))
    flags = _FakeTakeoverFlags(calls)
    config = cfg.Config()

    ok, msg = cli._takeover(
        store, tmp_path, config, controller, flags, _TAKEOVER_SID,
        max_wait=180.0, boot_id="test-boot",
        read_signal=lambda sid: {"mtime": 100.0, "tail_kind": "assistant-end"},
        clock=_scripted([500.0, 1000.0]), sleep=_failing_sleep,
    )
    assert not ok
    assert "failed to stop live pid 100" in msg
    assert ("arm_close", 50) in calls
    assert flags.armed == set()  # rolled back — no kill landed
    # adoption must never happen on a failed kill
    assert not any(
        e.get("claude", {}).get("session_id") == _TAKEOVER_SID for e in store.scan().entries
    )


def test_adopt_plain_delegates_to_the_existing_adopt_path(tmp_path, monkeypatch, capsys):
    # No --takeover: `crr adopt SID` must behave exactly like
    # `crr discover --adopt SID` (same message, same journal write).
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_discover_transcript(tmp_path / "home", "/home/u/proj", _DISCOVER_SID, [
        _discover_user_rec("a prompt", cwd="/home/u/proj"),
    ])
    rc = cli.main(["adopt", _DISCOVER_SID])
    out = capsys.readouterr().out
    assert rc == 0
    assert "adopted" in out
    assert _DISCOVER_SID[:8] in out

    store = JournalStore(tmp_path / "state")
    matches = [e for e in store.scan().entries if e["claude"]["session_id"] == _DISCOVER_SID]
    assert len(matches) == 1


def test_adopt_rejects_a_malformed_sid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    rc = cli.main(["adopt", "not-a-uuid"])
    assert rc == 2
    assert "not a valid session id" in capsys.readouterr().err


def test_adopt_takeover_wires_wait_flag_to_max_wait(tmp_path, monkeypatch, capsys):
    # `_cmd_adopt` must pass --wait through as _takeover's max_wait, and
    # fall back to config.takeover_max_wait_seconds when --wait is absent.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    recorded: list = []

    def fake_takeover(store, sd, config, controller, flags, sid, *, max_wait, **kw):
        recorded.append(max_wait)
        return True, "took over stub"

    monkeypatch.setattr(cli, "_takeover", fake_takeover)

    rc = cli.main(["adopt", _TAKEOVER_SID, "--takeover", "--wait", "7"])
    assert rc == 0
    assert "took over stub" in capsys.readouterr().out
    assert recorded[-1] == 7.0

    rc = cli.main(["adopt", _TAKEOVER_SID, "--takeover"])
    assert rc == 0
    assert recorded[-1] == cfg.Config().get("takeover_max_wait_seconds")


def test_adopt_takeover_notes_when_wait_is_below_idle_window(tmp_path, monkeypatch, capsys):
    # --wait 5 is below the default takeover_idle_seconds (20) — a quiet,
    # parked transcript can never reach the idle branch within 5s, so an
    # eventual "still actively writing after 5s" refusal would be
    # misleading (it wasn't writing). An honest up-front note must appear
    # on stderr, without blocking the attempt.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")

    def fake_takeover(store, sd, config, controller, flags, sid, *, max_wait, **kw):
        return True, "took over stub"

    monkeypatch.setattr(cli, "_takeover", fake_takeover)
    rc = cli.main(["adopt", _TAKEOVER_SID, "--takeover", "--wait", "5"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "--wait 5" in err
    assert "20" in err


def test_adopt_takeover_no_note_when_wait_meets_idle_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")

    def fake_takeover(store, sd, config, controller, flags, sid, *, max_wait, **kw):
        return True, "took over stub"

    monkeypatch.setattr(cli, "_takeover", fake_takeover)
    rc = cli.main(["adopt", _TAKEOVER_SID, "--takeover", "--wait", "30"])
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_adopt_takeover_refusal_prints_to_stderr_and_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")

    def fake_takeover(store, sd, config, controller, flags, sid, *, max_wait, **kw):
        return False, "refused stub"

    monkeypatch.setattr(cli, "_takeover", fake_takeover)
    rc = cli.main(["adopt", _TAKEOVER_SID, "--takeover"])
    assert rc == 1
    assert "refused stub" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Review fix-wave 2026-08-07, FIX 3 (IMPORTANT) — settings.json's global and
# per-session writers must share one lock around the WHOLE read-modify-write,
# not just each call's own atomic tmp-file-rename. `ThreadingHTTPServer`
# serves POSTs concurrently, so without this a per-session write can read a
# stale global value, then land AFTER a concurrent global-off write,
# silently reverting the panic switch back on.
#
# Sequential calls can never exhibit the lost update itself (there is no
# concurrency in a single thread to interleave), so instead these tests
# prove the load-bearing PRECONDITION for the fix: that the lock is held
# for the entire operation. A non-blocking probe acquisition of the SAME
# lock file, taken from inside a monkeypatched read hook that fires
# mid-operation (after the store's read, before its write), must fail with
# BlockingIOError — proving no other writer could land in that window.
# Deleting `with mutation_lock(sd):` from either wrapper turns this red.
# --------------------------------------------------------------------------

def _probe_lock_held(tmp_path) -> bool:
    """True if the shared mutation lock is currently held by someone else
    (probed via a fresh, independent file descriptor — both backends contend
    across descriptions/handles even within one process).

    Deliberately NOT skipped off POSIX. The invariant under test — the lock
    is held for the whole read-modify-write — is one crr claims on every
    platform, and locking.py's Windows backend (#70) has no other test that
    puts it under contention. Skipping here would hide exactly what that
    port introduced, so the probe dispatches instead: ``LK_NBLCK`` is the
    msvcrt spelling of ``LOCK_EX | LOCK_NB``, failing rather than waiting
    when the range is already taken.
    """
    from crr.adapters.locking import _LOCK_NAME
    fd = os.open(str(tmp_path / _LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_session_autokick_write_holds_the_lock_for_the_whole_operation(tmp_path, monkeypatch):
    from crr.core import settings

    held = []
    orig_read_raw = settings.SettingsStore._read_raw

    def probing_read_raw(self):
        # Fires from INSIDE write_session_autokick, after the lock (if any)
        # is acquired but before the write lands — exactly the window a
        # concurrent global write would need to land in for the bug.
        held.append(_probe_lock_held(tmp_path))
        return orig_read_raw(self)

    monkeypatch.setattr(settings.SettingsStore, "_read_raw", probing_read_raw)
    cli._write_session_autokick_locked(tmp_path, _TAKEOVER_SID, True)

    assert held == [True]  # the lock WAS held throughout — no window for the race


def test_global_autokick_write_holds_the_lock_for_the_whole_operation(tmp_path, monkeypatch):
    from crr.core import settings

    held = []
    orig_read_raw = settings.SettingsStore._read_raw

    def probing_read_raw(self):
        held.append(_probe_lock_held(tmp_path))
        return orig_read_raw(self)

    monkeypatch.setattr(settings.SettingsStore, "_read_raw", probing_read_raw)
    cli._write_global_autokick_locked(tmp_path, False)

    assert held == [True]


def test_unlocked_store_writes_do_not_hold_the_lock_control_case(tmp_path, monkeypatch):
    # Control: calling the STORE directly (bypassing the FIX 3 wrappers)
    # must NOT show the lock held — proves the probe technique above is
    # actually discriminating locked from unlocked, not a false positive.
    from crr.core import settings

    held = []
    orig_read_raw = settings.SettingsStore._read_raw

    def probing_read_raw(self):
        held.append(_probe_lock_held(tmp_path))
        return orig_read_raw(self)

    monkeypatch.setattr(settings.SettingsStore, "_read_raw", probing_read_raw)
    settings.SettingsStore(tmp_path).write_session_autokick(_TAKEOVER_SID, True)

    assert held == [False]


# --------------------------------------------------------------------------
# Review fix-wave 2026-08-07, FIX 4 (MINOR, same principle as b4fe3b6) —
# the Settings modal's checkbox must not render CHECKED while the store is
# degraded and every card renders `global-off` (b4fe3b6's fix covered the
# cards but not this provider).
# --------------------------------------------------------------------------

def test_settings_payload_resolved_is_false_when_degraded_even_if_config_default_true(tmp_path):
    from crr.core import settings as settings_mod
    (tmp_path / settings_mod.FILENAME).write_text("{not json", encoding="utf-8")
    config = cfg.Config({"remote_control_autokick": True})

    payload = cli._settings_payload(tmp_path, config)

    assert payload["degraded"] is True
    assert payload["resolved"] is False  # matches effective_global_autokick(), not a lying True


def test_settings_payload_resolved_falls_back_to_config_default_when_unset_and_healthy(tmp_path):
    config = cfg.Config({"remote_control_autokick": True})
    payload = cli._settings_payload(tmp_path, config)
    assert payload["degraded"] is False
    assert payload["resolved"] is True

    config_off = cfg.Config({"remote_control_autokick": False})
    payload_off = cli._settings_payload(tmp_path, config_off)
    assert payload_off["resolved"] is False


def test_settings_payload_resolved_reflects_a_healthy_stored_override(tmp_path):
    from crr.core import settings as settings_mod
    settings_mod.SettingsStore(tmp_path).write_global_autokick(False)
    config = cfg.Config({"remote_control_autokick": True})  # default True, override wins

    payload = cli._settings_payload(tmp_path, config)

    assert payload["degraded"] is False
    assert payload["resolved"] is False


def test_tab_spawner_uses_its_own_timeout_not_the_interop_one(monkeypatch):
    # [#53] interop_timeout_seconds (5s) is shared with ps/tmux probes, where
    # short is correct. A cold Windows Terminal start needs far longer, and
    # borrowing that budget is what produced false "NO TAB" reports.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows.shutil, "which", lambda b: "/mnt/c/wt.exe")
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows, "wt_probe", lambda path, timeout: True)
    config = cfg.Config()
    spawner, _expected = cli._tab_spawner(config)
    assert spawner._timeout == config.get("tab_spawn_timeout_seconds")
    assert spawner._timeout != config.get("interop_timeout_seconds")


def test_tab_spawn_timeout_default_covers_a_cold_terminal_launch():
    assert cfg.Config().get("tab_spawn_timeout_seconds") >= 20


def test_tab_spawner_resolves_the_distro_at_call_time_over_a_stale_env(monkeypatch):
    # [#54] crr systemd bakes WSL_DISTRO_NAME into the unit. After a distro
    # rename that baked value targets a distro that no longer exists; wslpath
    # reports the current name, so it must win.
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows, "wt_path", lambda: "/mnt/c/wt.exe")
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows, "wt_probe", lambda path, timeout: True)
    monkeypatch.setattr(cli.host, "_wslpath_root", lambda timeout=None: "\\\\wsl.localhost\\Renamed\\")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Stale-Name")
    spawner, _ = cli._tab_spawner(cfg.Config())
    assert spawner._distro == "Renamed"


def test_tab_spawner_falls_back_to_the_baked_env_when_wslpath_is_unavailable(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows, "wt_path", lambda: "/mnt/c/wt.exe")
    monkeypatch.setattr(cli.tab_spawn_windows, "interop_registered", lambda: True)
    monkeypatch.setattr(cli.tab_spawn_windows, "wt_probe", lambda path, timeout: True)
    monkeypatch.setattr(cli.host, "_wslpath_root", lambda timeout=None: None)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Baked-Name")
    spawner, _ = cli._tab_spawner(cfg.Config())
    assert spawner._distro == "Baked-Name"


def _parked_journal_entry(tmp_path, sid):
    """A pre-reboot entry (stale boot_id, dead pid) parked in a tmux session."""
    JournalStore(tmp_path).write(new_entry(
        pid=999999, cwd="/home/u/p", host="tmux", shell="bash",
        boot_id="a-previous-boot", now="2026-01-01T00:00:00+00:00",
        tmux_session="crr-8a1b2c3d",
        claude={"session_id": sid, "sid_source": "injected",
                "started": "2026-01-01T00:00:00+00:00",
                "skip_permissions": False}))


def test_status_json_reports_parked_for_a_tmux_restored_session(tmp_path, monkeypatch, capsys):
    # End to end through the composition root: a pre-reboot entry whose
    # tmux session is alive must not print as crashed.
    from crr.adapters import tmux

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _parked_journal_entry(tmp_path, sid)

    class FakeTmux:
        def available(self): return True
        def list_sessions(self): return {"crr-8a1b2c3d"}
        def attached_sessions(self): return set()  # parked, not yet reopened

    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: FakeTmux())
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["state"] == "parked"
    assert payload["sessions"][0]["attached"] is False


def test_status_json_declines_to_park_when_tmux_cannot_say(tmp_path, monkeypatch, capsys):
    # F16 tri-state at the composition root: list_sessions() -> None is
    # "could not determine" and must reach core as None, not as set(). A
    # helper that wrote `or set()` would pass the test above and still be
    # wrong here only in that it would... also say crashed. The value of
    # this pin is the inverse: it fails loudly if someone ever "fixes"
    # unknown into a promotion.
    from crr.adapters import tmux

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _parked_journal_entry(tmp_path, sid)

    class UnknownTmux:
        def available(self): return True
        def list_sessions(self): return None
        def attached_sessions(self): return None

    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: UnknownTmux())
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["state"] == "crashed"


def test_live_tmux_sessions_passes_the_unknown_tri_state_through(monkeypatch):
    # The helper itself: None must survive, and an absent tmux is a
    # confirmed-empty set(), not an unknown.
    from crr.adapters import tmux

    class UnknownTmux:
        def available(self): return True
        def list_sessions(self): return None

    class NoTmux:
        def available(self): return False
        def list_sessions(self): raise AssertionError("must not be queried")

    config = cfg.Config()
    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: UnknownTmux())
    assert cli._live_tmux_sessions(config) is None
    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: NoTmux())
    assert cli._live_tmux_sessions(config) == set()


def test_the_web_provider_reports_parked_for_a_tmux_restored_session(tmp_path, monkeypatch):
    # The dashboard surface, resolved per poll — not cached at server
    # start. A set hoisted out of provider() would freeze at boot and the
    # card would keep answering with a stale liveness forever.
    from crr.adapters import tmux

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _parked_journal_entry(tmp_path, sid)

    live = {"names": set()}

    class FakeTmux:
        def available(self): return True
        def list_sessions(self): return set(live["names"])
        def attached_sessions(self): return set()

    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: FakeTmux())
    captured = {}

    def fake_make_web_handler(provider, allowed, suffixes, **kw):
        captured["provider"] = provider
        return object()

    class _FakeServer:
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "make_web_handler", fake_make_web_handler)
    monkeypatch.setattr(cli, "ThreadingHTTPServer", _FakeServer)
    assert cli.main(["web", "--port", "1"]) == 0

    provider = captured["provider"]
    assert provider()["sessions"][0]["state"] == "crashed"
    live["names"] = {"crr-8a1b2c3d"}
    # Re-asked per poll: a set resolved once at startup would still say crashed.
    assert provider()["sessions"][0]["state"] == "parked"


def test_web_provider_includes_auth_state(tmp_path, monkeypatch):
    """The sessions payload from provider() must include the auth fields
    (spec 2026-08-21, dashboard reauth) — read fresh from the credentials
    file each poll, mirroring how reachability is re-asked each poll rather
    than resolved once at server start."""
    from crr.adapters import tmux

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _parked_journal_entry(tmp_path, sid)

    now = 1_700_000_000.0
    creds_path = tmp_path / ".credentials.json"
    # 4-day access expiry clears the 3-day "expiring" warning window (see
    # tests/test_auth.py::test_valid_both_tokens_fresh) — this test asserts
    # on the "valid" state specifically, not "expiring".
    creds_path.write_text(json.dumps({
        "expiresAt": int((now + 4 * 86400) * 1000),
        "refreshTokenExpiresAt": int((now + 30 * 86400) * 1000),
    }))
    monkeypatch.setattr("crr.cli._credentials_path", lambda _cfg: creds_path)
    monkeypatch.setattr("time.time", lambda: now)

    class FakeTmux:
        def available(self): return True
        def list_sessions(self): return set()
        def attached_sessions(self): return set()

    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: FakeTmux())
    captured = {}

    def fake_make_web_handler(provider, allowed, suffixes, **kw):
        captured["provider"] = provider
        return object()

    class _FakeServer:
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "make_web_handler", fake_make_web_handler)
    monkeypatch.setattr(cli, "ThreadingHTTPServer", _FakeServer)
    assert cli.main(["web", "--port", "1"]) == 0

    payload = captured["provider"]()
    assert payload["auth_state"] == "valid"
    assert isinstance(payload["auth_expires_in_seconds"], int)
    assert payload["auth_reauth_url"] is None


# --------------------------------------------------------------------------
# Dashboard reauth (spec 2026-08-21) — POST /api/reauth + /api/reauth-code
# provider closures inside `_cmd_web`, and the non-blocking flow through
# `provider()`: capture-pane polling for the OAuth URL, and firing
# `_post_reauth_recovery` on the expired -> valid transition.
#
# Driven through `cli.main(["web", ...])` with `make_web_handler` faked to
# capture every provider closure it's handed (mirrors
# `test_web_provider_includes_auth_state`, above) and `tmux.RealTmux`
# replaced by one shared `FakeTmux` instance, so calls made by the
# `tmux_spawner` `_cmd_web` builds once and by `_live_tmux_sessions`/
# `_attached_tmux_sessions` (which build a fresh `RealTmux` per poll) all
# land on the same fake — no real tmux is ever attached to, and no real
# subprocess is ever exec'd.
# --------------------------------------------------------------------------

class FakeReauthTmux:
    """Records every tmux call `_cmd_web`'s reauth flow can make."""

    def __init__(self, *, capture_output="", raise_on_new_session=False):
        self.available_ = True
        self.new_session_calls = []
        self.kill_session_calls = []
        self.capture_pane_calls = []
        self.send_keys_calls = []
        self._capture_output = capture_output
        self._raise_on_new_session = raise_on_new_session

    def available(self):
        return self.available_

    def list_sessions(self):
        return set()

    def attached_sessions(self):
        return set()

    def new_detached_session(self, name, cwd, argv):
        self.new_session_calls.append((name, cwd, list(argv)))
        if self._raise_on_new_session:
            raise subprocess.CalledProcessError(1, argv)

    def kill_session(self, name):
        self.kill_session_calls.append(name)

    def capture_pane(self, name):
        self.capture_pane_calls.append(name)
        return self._capture_output

    def send_keys(self, name, text):
        self.send_keys_calls.append((name, text))


def _web_captured(monkeypatch, tmp_path, fake_tmux=None):
    """Run `cli.main(["web", ...])` with `make_web_handler` faked so the
    test can call the real provider closures directly, exactly like
    `test_web_provider_includes_auth_state` above — `_FakeServer.
    serve_forever` raises `KeyboardInterrupt` immediately so `_cmd_web`
    completes (having already built every provider) instead of blocking.
    """
    from crr.adapters import tmux

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: fake_tmux or FakeReauthTmux())

    captured = {}

    def fake_make_web_handler(sessions_provider, allowed, suffixes, **kw):
        captured["provider"] = sessions_provider
        captured.update(kw)
        return object()

    class _FakeServer:
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "make_web_handler", fake_make_web_handler)
    monkeypatch.setattr(cli, "ThreadingHTTPServer", _FakeServer)
    assert cli.main(["web", "--port", "1"]) == 0
    return captured


def _dispatch_web(captured, method, path, headers=None, body=b"", query=""):
    """Replay a request through `web.handle_request` using the REAL
    provider closures `_cmd_web` built (captured via `_web_captured`) —
    same auth_enabled/setup_mode computation `make_web_handler`'s
    `_dispatch` does on every request, minus the actual socket. This is
    what makes the dashboard-login state-machine tests below end-to-end:
    a real `DashboardAuthStore` on `tmp_path` drives real closures, not
    hand-fed booleans."""
    from crr.core import web

    h = {"Host": "localhost"}
    if headers:
        h.update(headers)
    auth_enabled_fn = captured.get("auth_enabled_fn")
    setup_mode_fn = captured.get("setup_mode_fn")
    auth_enabled = bool(auth_enabled_fn()) if auth_enabled_fn else False
    setup_mode = bool(setup_mode_fn()) if setup_mode_fn else False
    bootstrap_state_fn = captured.get("bootstrap_state_fn")
    return web.handle_request(
        method, path, h, body,
        sessions_provider=captured["provider"],
        dashboard_auth_provider=captured.get("dashboard_auth_provider"),
        login_provider=captured.get("login_provider"),
        logout_provider=captured.get("logout_provider"),
        bootstrap_state=bootstrap_state_fn() if bootstrap_state_fn else None,
        auth_enabled=auth_enabled and captured.get("auth_check") is not None,
        auth_check=captured.get("auth_check"),
        setup_mode=setup_mode,
        allowed_hosts={"localhost", "127.0.0.1"},
        allowed_suffixes=(".ts.net",),
        query=query,
    )


def _write_creds(path, now, *, expired):
    """A credentials file that reads `auth.auth_state` as "expired" (both
    tokens past) or "valid" (both fresh, well outside the 3-day window)."""
    delta = -10.0 if expired else 4 * 86400
    path.write_text(json.dumps({
        "expiresAt": int((now + delta) * 1000),
        "refreshTokenExpiresAt": int((now + delta) * 1000),
    }))


def test_reauth_provider_spawns_tmux_session_nonblocking(tmp_path, monkeypatch):
    fake_tmux = FakeReauthTmux()
    captured = _web_captured(monkeypatch, tmp_path, fake_tmux)

    ok, message, degraded = captured["reauth_provider"]()

    assert ok is True
    assert "started" in message.lower()
    assert degraded is False
    assert len(fake_tmux.new_session_calls) == 1
    name, _cwd, argv = fake_tmux.new_session_calls[0]
    assert name == "crr-reauth"
    assert argv == ["claude", "auth", "login"]


def test_reauth_provider_rejects_concurrent(tmp_path, monkeypatch):
    fake_tmux = FakeReauthTmux()
    captured = _web_captured(monkeypatch, tmp_path, fake_tmux)

    ok1, _msg1, _deg1 = captured["reauth_provider"]()
    assert ok1 is True

    ok2, msg2, _deg2 = captured["reauth_provider"]()
    assert ok2 is False
    assert "already in progress" in msg2.lower()
    # Only the first call actually spawned a pane.
    assert len(fake_tmux.new_session_calls) == 1


def test_reauth_provider_resets_active_flag_when_tmux_fails(tmp_path, monkeypatch):
    """A tmux failure must not leave `_reauth_active` stuck True forever —
    the next reauth attempt must be allowed to proceed."""
    fake_tmux = FakeReauthTmux(raise_on_new_session=True)
    captured = _web_captured(monkeypatch, tmp_path, fake_tmux)

    ok1, msg1, _deg1 = captured["reauth_provider"]()
    assert ok1 is False
    assert "failed" in msg1.lower()

    fake_tmux._raise_on_new_session = False
    ok2, _msg2, _deg2 = captured["reauth_provider"]()
    assert ok2 is True


def test_reauth_url_captured_on_poll(tmp_path, monkeypatch):
    """After `reauth_provider()` starts the pane, `provider()`'s next call
    captures the URL out of the pane text via `_poll_reauth_url_once` and
    surfaces it as `auth_reauth_url` — using `-J` join semantics so a
    wrapped OAuth URL is not truncated (spike data, task-4 brief)."""
    spike_output = (
        "Opening browser to sign in…\n"
        "If the browser didn't open, visit: "
        "https://claude.com/cai/oauth/authorize?code=true&client_id=FAKE\n"
        "Paste code here if prompted > \n"
    )
    fake_tmux = FakeReauthTmux(capture_output=spike_output)
    captured = _web_captured(monkeypatch, tmp_path, fake_tmux)

    ok, _msg, _deg = captured["reauth_provider"]()
    assert ok is True

    payload = captured["provider"]()
    assert payload["auth_reauth_url"] == (
        "https://claude.com/cai/oauth/authorize?code=true&client_id=FAKE"
    )
    assert fake_tmux.capture_pane_calls == ["crr-reauth"]

    # Once captured, later polls do not re-capture (nothing left to poll for).
    payload2 = captured["provider"]()
    assert payload2["auth_reauth_url"] == payload["auth_reauth_url"]
    assert fake_tmux.capture_pane_calls == ["crr-reauth"]


def test_reauth_url_is_none_without_an_active_reauth(tmp_path, monkeypatch):
    captured = _web_captured(monkeypatch, tmp_path)
    payload = captured["provider"]()
    assert payload["auth_reauth_url"] is None


def test_reauth_code_provider_sends_keys_nonblocking(tmp_path, monkeypatch):
    fake_tmux = FakeReauthTmux()
    captured = _web_captured(monkeypatch, tmp_path, fake_tmux)

    captured["reauth_provider"]()
    ok, message, degraded = captured["reauth_code_provider"]("abc123")

    assert ok is True
    assert "submitted" in message.lower()
    assert degraded is False
    assert fake_tmux.send_keys_calls == [("crr-reauth", "abc123")]


def test_reauth_code_provider_without_active_reauth_fails(tmp_path, monkeypatch):
    fake_tmux = FakeReauthTmux()
    captured = _web_captured(monkeypatch, tmp_path, fake_tmux)

    ok, message, _degraded = captured["reauth_code_provider"]("abc123")
    assert ok is False
    assert "no reauth in progress" in message.lower()
    assert fake_tmux.send_keys_calls == []


def test_post_reauth_recovery_fires_once_on_expired_to_valid_transition(tmp_path, monkeypatch):
    """`provider()` must call `_post_reauth_recovery` exactly once, on the
    FIRST poll where auth flips expired -> valid while a reauth is active
    — never on a poll where no reauth was active (a token that simply
    expired and was refreshed outside the dashboard is not this flow), and
    never a second time on a later poll (idempotent)."""
    fake_tmux = FakeReauthTmux()
    captured = _web_captured(monkeypatch, tmp_path, fake_tmux)

    now = 1_700_000_000.0
    creds_path = tmp_path / ".credentials.json"
    monkeypatch.setattr(cli, "_credentials_path", lambda _cfg: creds_path)
    monkeypatch.setattr("time.time", lambda: now)

    calls = []
    monkeypatch.setattr(cli, "_post_reauth_recovery", lambda *a, **kw: calls.append(1))

    # 1. Auth expired, no reauth active yet: recovery must not fire.
    _write_creds(creds_path, now, expired=True)
    payload = captured["provider"]()
    assert payload["auth_state"] == "expired"
    assert calls == []

    # 2. User starts the reauth flow from the dashboard.
    ok, _msg, _deg = captured["reauth_provider"]()
    assert ok is True

    # 3. Still expired (login not finished): no recovery yet.
    captured["provider"]()
    assert calls == []

    # 4. Credentials refresh -> auth flips to valid: recovery fires exactly once.
    _write_creds(creds_path, now, expired=False)
    captured["provider"]()
    assert calls == [1]

    # 5. A further poll, still valid: must not fire again.
    captured["provider"]()
    assert calls == [1]
    # The pane is torn down as part of cleanup.
    assert "crr-reauth" in fake_tmux.kill_session_calls


# --------------------------------------------------------------------------
# `_post_reauth_recovery` / `_do_kick` — the post-reauth sweep fired from
# `provider()` on the expired -> valid transition (tested above through
# `_post_reauth_recovery` being called). Exercised directly here, mirroring
# `tests/test_revive_bridge.py`'s fakes for `_kick_dropped_bridges`: a fake
# boot/probe/controller decide classifier + reachability, `ops.reopen` and
# `_do_kick` are monkeypatched so no real process is ever touched.
# --------------------------------------------------------------------------

def test_post_reauth_recovery_resets_counters_reopens_crashed_kicks_unreachable(tmp_path, monkeypatch):
    """Resets EVERY claude-bearing session's kick counters (an attempt cap
    exhausted while auth was expired must not block recovery), reopens the
    CRASHED session, and kicks the LIVE-and-unreachable-and-idle one — but
    leaves a LIVE-and-unreachable-but-BUSY session alone
    (`reachability.may_kick`), same guard `_kick_dropped_bridges` uses to
    avoid destroying work in flight."""
    from crr.adapters import session_state, transcript_source
    from crr.core import bridge_kicks, ops
    from crr.core.flags import FlagStore

    sd = tmp_path
    store = JournalStore(sd)
    archive = ArchiveStore(sd)
    flags = FlagStore(sd)
    _BOOT = "current-boot"

    sid_crashed = "8a1b2c3d-0000-4a6b-8c7d-9e0f1a2b3c4d"
    sid_kicked = "8a1b2c3d-0001-4a6b-8c7d-9e0f1a2b3c4d"
    sid_reachable = "8a1b2c3d-0002-4a6b-8c7d-9e0f1a2b3c4d"
    sid_busy = "8a1b2c3d-0003-4a6b-8c7d-9e0f1a2b3c4d"

    def _write(pid, sid, boot_id):
        store.write(new_entry(
            pid=pid, cwd="/home/u/project", host="tmux", shell="zsh",
            boot_id=boot_id, now="2026-08-21T00:00:00Z",
            claude={"session_id": sid, "sid_source": "injected",
                    "started": "2026-08-21T00:00:00Z",
                    "skip_permissions": False}))

    _write(1001, sid_crashed, "a-previous-boot")  # boot mismatch -> CRASHED
    _write(1002, sid_kicked, _BOOT)               # LIVE, unreachable, idle
    _write(1003, sid_reachable, _BOOT)            # LIVE, reachable
    _write(1004, sid_busy, _BOOT)                 # LIVE, unreachable, busy

    class _Boot:
        def current(self):
            return _BOOT

    class _Probe:
        def is_alive(self, pid):
            return True

        def has_controlling_tty(self, pid):
            return True

    class _Controller:
        def claude_groups(self, pid):
            return [pid + 5000]  # arbitrary claude pgid, distinct from the shell pid

    states = {
        sid_kicked: session_state.SessionState(
            pid=1002 + 5000, bridge_session_id=None, field_present=True,
            status="idle", waiting_for=""),
        sid_reachable: session_state.SessionState(
            pid=1003 + 5000, bridge_session_id="some-bridge-id", field_present=True,
            status="idle", waiting_for=""),
        sid_busy: session_state.SessionState(
            pid=1004 + 5000, bridge_session_id=None, field_present=True,
            status="busy", waiting_for=""),
    }
    monkeypatch.setattr(session_state, "read_all", lambda: states)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))

    # Idle session at a clean boundary (assistant-end, long idle) → eligible.
    monkeypatch.setattr(transcript_source, "read_takeover_signal",
                         lambda sid, home=None: {"mtime": 0.0, "tail_kind": "assistant-end"})
    # time.time() - 0.0 >> default idle_window (20s), so ready_to_take_over → True.

    reset_sids = []
    monkeypatch.setattr(bridge_kicks.KickHistoryStore, "reset",
                         lambda self, sid, now=None: reset_sids.append(sid))

    reopened_pids = []

    def fake_reopen(*a, **kw):
        reopened_pids.append(a[7])  # (store, archive, tmux, controller, flags, boot, probe, pid, now)
        return ops.OpResult(True, "reopened")
    monkeypatch.setattr(ops, "reopen", fake_reopen)

    kicked_pids = []
    monkeypatch.setattr(cli, "_do_kick", lambda entry, *a, **kw: kicked_pids.append(entry["pid"]))

    cli._post_reauth_recovery(
        store, archive, _Boot(), _Probe(), _Controller(), flags,
        cfg.Config(), sd, tmux_spawner=None,
    )

    assert set(reset_sids) == {sid_crashed, sid_kicked, sid_reachable, sid_busy}
    assert reopened_pids == [1001]
    assert kicked_pids == [1002]


def test_post_reauth_recovery_skips_idle_session_mid_turn(tmp_path, monkeypatch):
    """An idle session whose transcript tail is mid-turn (not at an
    ``assistant-end`` boundary) must NOT be kicked by recovery — mirroring
    the same two-signal corroboration that ``_kick_dropped_bridges`` uses
    (``takeover.ready_to_take_over``). Without this guard, a long
    non-streaming completion could be SIGTERM'd mid-output."""
    from crr.adapters import session_state, transcript_source
    from crr.core import bridge_kicks, ops
    from crr.core.flags import FlagStore

    sd = tmp_path
    store = JournalStore(sd)
    archive = ArchiveStore(sd)
    flags = FlagStore(sd)
    _BOOT = "current-boot"

    sid_idle_midturn = "8a1b2c3d-0010-4a6b-8c7d-9e0f1a2b3c4d"

    store.write(new_entry(
        pid=2001, cwd="/home/u/project", host="tmux", shell="zsh",
        boot_id=_BOOT, now="2026-08-21T00:00:00Z",
        claude={"session_id": sid_idle_midturn, "sid_source": "injected",
                "started": "2026-08-21T00:00:00Z",
                "skip_permissions": False}))

    class _Boot:
        def current(self):
            return _BOOT

    class _Probe:
        def is_alive(self, pid):
            return True

        def has_controlling_tty(self, pid):
            return True

    class _Controller:
        def claude_groups(self, pid):
            return [pid + 5000]

    states = {
        sid_idle_midturn: session_state.SessionState(
            pid=2001 + 5000, bridge_session_id=None, field_present=True,
            status="idle", waiting_for=""),
    }
    monkeypatch.setattr(session_state, "read_all", lambda: states)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: (None, False))

    # Transcript tail is mid-turn — ready_to_take_over will return False.
    monkeypatch.setattr(transcript_source, "read_takeover_signal",
                         lambda sid, home=None: {"mtime": 0.0, "tail_kind": "mid-turn"})

    monkeypatch.setattr(bridge_kicks.KickHistoryStore, "reset",
                         lambda self, sid, now=None: None)

    kicked_pids = []
    monkeypatch.setattr(cli, "_do_kick", lambda entry, *a, **kw: kicked_pids.append(entry["pid"]))

    cli._post_reauth_recovery(
        store, archive, _Boot(), _Probe(), _Controller(), flags,
        cfg.Config(), sd, tmux_spawner=None,
    )

    # The idle mid-turn session must NOT be kicked.
    assert kicked_pids == []


# --------------------------------------------------------------------------
# Dashboard login wiring (spec 2026-08-26, Task 5) — the closures `_cmd_web`
# builds around `dashboard_auth.DashboardAuthStore` and hands to
# `make_web_handler`, captured through `_web_captured` exactly like the
# reauth providers above.
# --------------------------------------------------------------------------

def test_auth_enabled_fn_flips_live_after_enable_no_restart_needed(tmp_path, monkeypatch):
    """Enabling login through `dashboard_auth_provider` must be visible to
    `auth_enabled_fn` on the very next call — this is the "live per-request"
    choice (docstring on `make_web_handler`): no service restart to pick up
    a passphrase set from the Settings/bootstrap modal."""
    captured = _web_captured(monkeypatch, tmp_path)

    assert captured["auth_enabled_fn"]() is False

    ok, message = captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}
    )
    assert ok is True
    assert message == "Login enabled"
    assert captured["auth_enabled_fn"]() is True


def test_login_provider_sets_cookie_with_expected_attributes(tmp_path, monkeypatch):
    captured = _web_captured(monkeypatch, tmp_path)
    captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}
    )

    ok, error, headers = captured["login_provider"]("x" * 8)

    assert ok is True
    assert error == ""
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{dashboard_auth.COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie  # loopback/tailnet-only; no TLS termination here
    session_hours = cfg.Config().get("dashboard_session_hours")
    assert f"Max-Age={session_hours * 3600}" in cookie


def test_login_provider_wrong_passphrase_is_rejected_and_rate_limited(tmp_path, monkeypatch):
    captured = _web_captured(monkeypatch, tmp_path)
    captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}
    )

    for _ in range(5):
        ok, error, headers = captured["login_provider"]("wrong-pass")
        assert ok is False
        assert error == "Incorrect passphrase"
        assert headers == {}

    # Past the threshold, a wrong passphrase still fails — but now via
    # delay-then-verify, not an early "too many attempts" return (that
    # message promised a wait no timestamp ever enforced). The delay is the
    # only defense, so verification must still happen after sleeping.
    sleeps = []
    monkeypatch.setattr("crr.cli.time.sleep", lambda s: sleeps.append(s))
    ok, error, _headers = captured["login_provider"]("wrong-pass")
    assert ok is False
    assert error == "Incorrect passphrase"
    assert sleeps and sleeps[0] > 0

    # Backoff keeps growing on the rate-limited path too — record_failure()
    # must still run, not be skipped because we're already over threshold.
    ok2, _error2, _headers2 = captured["login_provider"]("wrong-pass")
    assert ok2 is False
    assert sleeps[1] > sleeps[0]


def test_login_provider_correct_passphrase_succeeds_past_rate_limit_threshold(tmp_path, monkeypatch):
    """Regression for the critical finding: once `_failures` reaches 5, the
    ONLY way it ever decreases is `reset()` on a successful verify. An
    early-return "too many attempts" path that skips verification entirely
    would reject the correct passphrase forever (until a service restart) —
    this must not happen; the delay is server-side punishment, not a lockout."""
    captured = _web_captured(monkeypatch, tmp_path)
    captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "correct-horse-battery", "confirm": "correct-horse-battery"}
    )

    monkeypatch.setattr("crr.cli.time.sleep", lambda s: None)
    for _ in range(6):
        ok, _error, _headers = captured["login_provider"]("wrong-pass")
        assert ok is False

    ok, error, headers = captured["login_provider"]("correct-horse-battery")
    assert ok is True
    assert error == ""
    assert "Set-Cookie" in headers

    # And the rate limiter is now reset — a subsequent wrong guess is not
    # immediately rate-limited.
    sleeps = []
    monkeypatch.setattr("crr.cli.time.sleep", lambda s: sleeps.append(s))
    ok2, error2, _headers2 = captured["login_provider"]("wrong-again")
    assert ok2 is False
    assert error2 == "Incorrect passphrase"
    assert sleeps == []


def test_login_provider_reset_on_success_clears_rate_limit(tmp_path, monkeypatch):
    captured = _web_captured(monkeypatch, tmp_path)
    captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}
    )
    for _ in range(4):
        captured["login_provider"]("wrong-pass")

    ok, _error, _headers = captured["login_provider"]("x" * 8)
    assert ok is True

    # A correct login resets the failure counter — the next wrong guess is
    # NOT immediately rate-limited.
    ok2, error2, _headers2 = captured["login_provider"]("still-wrong")
    assert ok2 is False
    assert error2 == "Incorrect passphrase"


def test_auth_check_round_trips_a_token_issued_by_login_provider(tmp_path, monkeypatch):
    captured = _web_captured(monkeypatch, tmp_path)
    captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}
    )
    _ok, _error, headers = captured["login_provider"]("x" * 8)
    cookie = headers["Set-Cookie"]
    token = cookie.split(";")[0].split("=", 1)[1]

    assert captured["auth_check"](token) is True
    assert captured["auth_check"]("garbage") is False


def test_auth_check_fails_closed_when_login_never_enabled(tmp_path, monkeypatch):
    """No signing secret has ever been minted -> every token is invalid,
    never a crash (`signing_secret()` returning None short-circuits)."""
    captured = _web_captured(monkeypatch, tmp_path)
    assert captured["auth_check"]("anything") is False


def test_dashboard_auth_provider_passphrase_error_becomes_ok_false_not_raise(tmp_path, monkeypatch):
    """PassphraseError (bad current passphrase, mismatch, too short) must
    come back as (False, message) — NOT propagate — so `web.handle_request`
    emits the {"ok": false, "message": ...} JSON contract the Settings/
    bootstrap modal JS parses with `r.json()`, rather than a plain-text 400
    that would throw in the browser."""
    captured = _web_captured(monkeypatch, tmp_path)
    captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}
    )

    ok, message = captured["dashboard_auth_provider"](
        {"op": "change", "current": "wrong", "new": "y" * 8, "confirm": "y" * 8}
    )
    assert ok is False
    assert "incorrect" in message.lower()

    # "enable" is now guarded against overwriting an already-enabled login
    # (Finding 2) — disable first so this exercises the short-passphrase
    # validation on a fresh "enable", not the "already enabled" guard.
    captured["dashboard_auth_provider"]({"op": "disable", "current": "x" * 8})
    ok2, message2 = captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "short", "confirm": "short"}
    )
    assert ok2 is False
    assert "8" in message2


def test_dashboard_auth_provider_enable_rejected_when_already_enabled(tmp_path, monkeypatch):
    """An already-authenticated client (e.g. a stolen but still-valid
    session cookie) POSTing {"op": "enable", ...} must not be able to
    silently overwrite the passphrase and signing secret without proving
    knowledge of the current passphrase — that would lock the real owner
    out of every open session. "change" is the only way to rotate once
    login is on."""
    captured = _web_captured(monkeypatch, tmp_path)
    ok, _msg = captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "original-pass", "confirm": "original-pass"}
    )
    assert ok is True

    ok2, message2 = captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "attacker-pass", "confirm": "attacker-pass"}
    )
    assert ok2 is False
    assert "already enabled" in message2.lower()

    # The original passphrase must still work — it was never overwritten.
    ok3, _error3, _headers3 = captured["login_provider"]("original-pass")
    assert ok3 is True


def test_dashboard_auth_provider_disable_transitions_straight_to_opted_out(tmp_path, monkeypatch):
    """Disabling IS opting out (state-machine fix, spec 2026-08-26
    revision): `disable()` must also set bootstrap_dismissed=True, or the
    user who just authenticated to turn login off would land back in
    UNDECIDED and be immediately re-blocked by the setup gate on their very
    next request — turning "disable" into a self-lockout."""
    captured = _web_captured(monkeypatch, tmp_path)
    captured["dashboard_auth_provider"](
        {"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}
    )
    assert captured["bootstrap_state_fn"]()["login_enabled"] is True

    ok, _msg = captured["dashboard_auth_provider"]({"op": "disable", "current": "x" * 8})
    assert ok is True
    state = captured["bootstrap_state_fn"]()
    assert state["login_enabled"] is False
    assert state["bootstrap_dismissed"] is True


def test_dashboard_auth_provider_dismiss_bootstrap_from_undecided(tmp_path, monkeypatch):
    captured = _web_captured(monkeypatch, tmp_path)
    assert captured["bootstrap_state_fn"]()["bootstrap_dismissed"] is False

    ok, _msg = captured["dashboard_auth_provider"]({"op": "dismiss-bootstrap"})
    assert ok is True
    assert captured["bootstrap_state_fn"]()["bootstrap_dismissed"] is True


def test_logout_provider_clears_the_cookie(tmp_path, monkeypatch):
    captured = _web_captured(monkeypatch, tmp_path)
    headers = captured["logout_provider"]()
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{dashboard_auth.COOKIE_NAME}=;")
    assert "Max-Age=0" in cookie


def test_auth_enabled_fn_is_true_when_store_is_corrupt(tmp_path, monkeypatch):
    """Fail-closed revision (user decision 2026-08-26, replacing the
    original fail-open spec): a corrupt `dashboard_auth.json` must activate
    the gate, not disable it — `auth_enabled_fn` is `login_enabled() OR
    is_corrupt()`."""
    (tmp_path / dashboard_auth.FILENAME).write_text("not json")
    captured = _web_captured(monkeypatch, tmp_path)
    assert captured["auth_enabled_fn"]() is True


def test_auth_enabled_fn_is_false_when_store_is_absent(tmp_path, monkeypatch):
    """Regression for state 1 (fresh install / upgrade): no file at all
    must NOT trip the corrupt-store gate."""
    captured = _web_captured(monkeypatch, tmp_path)
    assert captured["auth_enabled_fn"]() is False


def test_auth_check_returns_false_when_store_is_corrupt(tmp_path, monkeypatch):
    (tmp_path / dashboard_auth.FILENAME).write_text("not json")
    captured = _web_captured(monkeypatch, tmp_path)
    assert captured["auth_check"]("anything") is False


def test_login_provider_reports_corrupt_store_before_verifying(tmp_path, monkeypatch):
    """A login attempt against a corrupt store must surface the real
    reason — not "Incorrect passphrase" — and must not touch the rate
    limiter (it's not a guess, so no `record_failure`, no sleep)."""
    (tmp_path / dashboard_auth.FILENAME).write_text("not json")
    captured = _web_captured(monkeypatch, tmp_path)

    sleeps = []
    monkeypatch.setattr("crr.cli.time.sleep", lambda s: sleeps.append(s))
    ok, error, headers = captured["login_provider"]("anything")

    assert ok is False
    assert error == (
        "Auth store is corrupted — repair or delete dashboard_auth.json on the server."
    )
    assert headers == {}
    assert sleeps == []


def test_login_provider_reports_not_configured_on_a_fresh_store(tmp_path, monkeypatch):
    """UNDECIDED/OPTED_OUT (spec 2026-08-26 revision): no passphrase has
    ever been set, so `POST /api/login` must say so — not fall through to
    `verify()` (which would fail against a nonexistent hash and misreport
    as "Incorrect passphrase") — and, like the corrupt-store case above,
    this isn't a guessed passphrase: it must not touch the rate limiter
    (no `record_failure`, no `sleep`) or a fresh install's first mistyped
    login attempt would already be burning rate-limit budget."""
    captured = _web_captured(monkeypatch, tmp_path)  # no enable(), no corrupt

    sleeps = []
    monkeypatch.setattr("crr.cli.time.sleep", lambda s: sleeps.append(s))
    ok, error, headers = captured["login_provider"]("anything")

    assert ok is False
    assert error == "Login not configured"
    assert headers == {}
    assert sleeps == []


def test_bootstrap_state_fn_reports_login_enabled_true_when_corrupt(tmp_path, monkeypatch):
    """Corrupt must not show the bootstrap prompt: the front end shows it
    only when `login_enabled === false && bootstrap_dismissed === false`
    (page.html). The gate already blocks /api/sessions regardless, but the
    reported state must not accidentally invite bootstrap."""
    (tmp_path / dashboard_auth.FILENAME).write_text("not json")
    captured = _web_captured(monkeypatch, tmp_path)
    state = captured["bootstrap_state_fn"]()
    assert state["login_enabled"] is True


def test_auth_enabled_cannot_reach_handle_request_without_a_live_auth_check(tmp_path, monkeypatch):
    """Obligation from the Task 4 review: `auth_enabled=True` must never
    reach `web.handle_request` alongside `auth_check=None` (that combination
    silently fails open there). `make_web_handler` closes this at the one
    choke point every request passes through — prove it end to end over a
    real socket rather than trusting the wiring by inspection."""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    payload = {"contract": 1, "sessions": []}
    handler = cli.make_web_handler(
        lambda: payload, {"localhost", "127.0.0.1"}, (".ts.net",),
        auth_enabled_fn=lambda: True,  # login IS enabled...
        auth_check=None,               # ...but no way to validate a cookie
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sessions",
                                     headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=5) as r:
            # Must NOT fail open to a login page / 401 — falls back to
            # ungated (same as auth_enabled_fn absent entirely), which is a
            # deliberately narrow, documented degradation, not a crash.
            assert r.status == 200
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# Dashboard login state machine (spec 2026-08-26 revision: default-secure
# bootstrap — auth is opt-out, not opt-in). Four states — UNDECIDED,
# OPTED_OUT, ENABLED, CORRUPT — driven end to end through the real
# `_cmd_web` closures (`_web_captured`) and `web.handle_request`
# (`_dispatch_web`), with a real `DashboardAuthStore` on `tmp_path`.
# --------------------------------------------------------------------------

_JSON_HDR = {"Content-Type": "application/json"}


class TestDashboardLoginStateMachine:
    def test_fresh_install_serves_the_setup_page_not_the_dashboard(self, tmp_path, monkeypatch):
        captured = _web_captured(monkeypatch, tmp_path)
        resp = _dispatch_web(captured, "GET", "/")
        assert resp.status == 200
        assert b"Secure this dashboard" in resp.body
        assert b'id="sessions"' not in resp.body

    def test_fresh_install_blocks_the_api(self, tmp_path, monkeypatch):
        captured = _web_captured(monkeypatch, tmp_path)
        resp = _dispatch_web(captured, "GET", "/api/sessions")
        assert resp.status == 401
        assert json.loads(resp.body) == {"error": "unauthorized"}

    def test_fresh_install_exempts_version_and_pwa_assets(self, tmp_path, monkeypatch):
        captured = _web_captured(monkeypatch, tmp_path)
        for path in ("/api/version", "/manifest.webmanifest", "/sw.js"):
            resp = _dispatch_web(captured, "GET", path)
            assert resp.status == 200, path

    def test_enable_unauthenticated_transitions_to_enabled(self, tmp_path, monkeypatch):
        captured = _web_captured(monkeypatch, tmp_path)
        resp = _dispatch_web(
            captured, "POST", "/api/dashboard-auth", headers=_JSON_HDR,
            body=json.dumps({"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}).encode(),
        )
        assert resp.status == 200
        assert json.loads(resp.body)["ok"] is True

        # Subsequent unauthenticated request now sees the LOGIN page, not
        # the setup page — the state transitioned all the way to ENABLED,
        # live, with no service restart.
        resp2 = _dispatch_web(captured, "GET", "/")
        assert resp2.status == 200
        assert b"Secure this dashboard" not in resp2.body
        assert b"passphrase" in resp2.body.lower()
        assert b"Log in" in resp2.body

        resp3 = _dispatch_web(captured, "GET", "/api/sessions")
        assert resp3.status == 401

    def test_dismiss_bootstrap_transitions_to_opted_out(self, tmp_path, monkeypatch):
        captured = _web_captured(monkeypatch, tmp_path)
        resp = _dispatch_web(
            captured, "POST", "/api/dashboard-auth", headers=_JSON_HDR,
            body=json.dumps({"op": "dismiss-bootstrap"}).encode(),
        )
        assert resp.status == 200
        assert json.loads(resp.body)["ok"] is True

        # OPTED_OUT: the gate is off entirely — exactly today's disabled
        # behavior, dashboard open.
        resp2 = _dispatch_web(captured, "GET", "/")
        assert resp2.status == 200
        assert b'id="sessions"' in resp2.body
        assert b"Secure this dashboard" not in resp2.body

        resp3 = _dispatch_web(captured, "GET", "/api/sessions")
        assert resp3.status == 200

    def test_undecided_rejects_change_and_disable_explicitly(self, tmp_path, monkeypatch):
        captured = _web_captured(monkeypatch, tmp_path)
        for op, extra in [
            ("change", {"current": "x", "new": "y" * 8, "confirm": "y" * 8}),
            ("disable", {"current": "x"}),
        ]:
            resp = _dispatch_web(
                captured, "POST", "/api/dashboard-auth", headers=_JSON_HDR,
                body=json.dumps({"op": op, **extra}).encode(),
            )
            assert resp.status == 400, op
            body = json.loads(resp.body)
            assert body["ok"] is False, op

        # Neither rejected op touched the store: it's still UNDECIDED, and
        # "enable" still works afterward.
        resp = _dispatch_web(captured, "GET", "/")
        assert b"Secure this dashboard" in resp.body

    def test_corrupt_store_blocks_setup_ops_and_fails_closed(self, tmp_path, monkeypatch):
        (tmp_path / dashboard_auth.FILENAME).write_text("not json")
        captured = _web_captured(monkeypatch, tmp_path)

        # Setup ops must NOT be reachable — a corrupt store must not be
        # overwritable remotely, even via the op an UNDECIDED first-visitor
        # could otherwise use unauthenticated.
        resp = _dispatch_web(
            captured, "POST", "/api/dashboard-auth", headers=_JSON_HDR,
            body=json.dumps({"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}).encode(),
        )
        assert resp.status == 401

        resp2 = _dispatch_web(
            captured, "POST", "/api/dashboard-auth", headers=_JSON_HDR,
            body=json.dumps({"op": "dismiss-bootstrap"}).encode(),
        )
        assert resp2.status == 401

        # GET / fails closed exactly as today: the login page, never the
        # setup page (which would invite "fixing" the corrupt store).
        resp3 = _dispatch_web(captured, "GET", "/")
        assert resp3.status == 200
        assert b"Secure this dashboard" not in resp3.body
        assert b"passphrase" in resp3.body.lower()

        resp4 = _dispatch_web(captured, "GET", "/api/sessions")
        assert resp4.status == 401

    def test_authenticated_disable_serves_dashboard_not_setup_page_next(self, tmp_path, monkeypatch):
        """disable() -> bootstrap_dismissed=True (OPTED_OUT), end to end: an
        authenticated disable must not dump the user back into UNDECIDED and
        re-block the dashboard on their very next request."""
        captured = _web_captured(monkeypatch, tmp_path)
        _dispatch_web(
            captured, "POST", "/api/dashboard-auth", headers=_JSON_HDR,
            body=json.dumps({"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}).encode(),
        )
        login_resp = _dispatch_web(
            captured, "POST", "/api/login", headers=_JSON_HDR,
            body=json.dumps({"passphrase": "x" * 8}).encode(),
        )
        assert login_resp.status == 200
        cookie_value = login_resp.headers["Set-Cookie"].split(";")[0]

        resp = _dispatch_web(
            captured, "POST", "/api/dashboard-auth",
            headers={**_JSON_HDR, "Cookie": cookie_value},
            body=json.dumps({"op": "disable", "current": "x" * 8}).encode(),
        )
        assert resp.status == 200
        assert json.loads(resp.body)["ok"] is True

        # Next request (no cookie needed — the gate is off now): dashboard,
        # not the setup page.
        resp2 = _dispatch_web(captured, "GET", "/")
        assert resp2.status == 200
        assert b'id="sessions"' in resp2.body
        assert b"Secure this dashboard" not in resp2.body

    def test_disable_clears_credential_material_old_passphrase_no_longer_logs_in(
        self, tmp_path, monkeypatch,
    ):
        """Security-hygiene fix: once disabled, the old passphrase must not
        still work against `/api/login` — that would be a live, rate-limited
        oracle for a passphrase the user believes they turned off, and it
        would mint a real (if useless) session cookie. After disable, a
        login attempt with the old passphrase must get the same "Login not
        configured" answer as a fresh install, and no Set-Cookie."""
        captured = _web_captured(monkeypatch, tmp_path)
        _dispatch_web(
            captured, "POST", "/api/dashboard-auth", headers=_JSON_HDR,
            body=json.dumps({"op": "enable", "passphrase": "x" * 8, "confirm": "x" * 8}).encode(),
        )
        login_resp = _dispatch_web(
            captured, "POST", "/api/login", headers=_JSON_HDR,
            body=json.dumps({"passphrase": "x" * 8}).encode(),
        )
        cookie_value = login_resp.headers["Set-Cookie"].split(";")[0]

        disable_resp = _dispatch_web(
            captured, "POST", "/api/dashboard-auth",
            headers={**_JSON_HDR, "Cookie": cookie_value},
            body=json.dumps({"op": "disable", "current": "x" * 8}).encode(),
        )
        assert json.loads(disable_resp.body)["ok"] is True

        resp = _dispatch_web(
            captured, "POST", "/api/login", headers=_JSON_HDR,
            body=json.dumps({"passphrase": "x" * 8}).encode(),
        )
        assert resp.status == 401
        assert json.loads(resp.body) == {"error": "Login not configured"}
        assert "Set-Cookie" not in resp.headers


def test_do_kick_records_the_attempt_even_when_ops_kick_raises(tmp_path, monkeypatch):
    """`_do_kick` must count the attempt even on an exception — same
    reasoning as `_kick_dropped_bridges`'s own `finally`: a silently
    uncounted attempt would let a later sweep retry with no memory of this
    one, reopening the restart-loop hole the attempt cap exists to close."""
    from crr.core import bridge_kicks, ops
    from crr.core.flags import FlagStore

    sd = tmp_path
    store = JournalStore(sd)
    flags = FlagStore(sd)
    kick_store = bridge_kicks.KickHistoryStore(sd)
    sid = "8a1b2c3d-0000-4a6b-8c7d-9e0f1a2b3c4d"
    entry = {"pid": 42, "claude": {"session_id": sid}}

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(ops, "kick", boom)
    with pytest.raises(RuntimeError):
        cli._do_kick(entry, kick_store, cfg.Config(), boot=None, probe=None,
                     controller=None, flags=flags, store=store)

    assert kick_store.attempts(sid) == 1


def test_status_human_says_restored_not_the_raw_parked_enum(tmp_path, monkeypatch, capsys):
    """One state, one word, on every surface.

    `parked` is the contract value; `restored` is what a human is told. The
    dashboard already renames it, so without this the text CLI and the
    dashboard describe the same session with two different words — the exact
    kind of surface-dependent inconsistency Phase 0 exists to remove.
    """
    from crr.adapters import state_dir, tmux
    from crr.core.journal import JournalStore, new_entry

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    JournalStore(tmp_path).write(new_entry(
        pid=999999, cwd="/home/u/p", host="tmux", shell="bash",
        boot_id="a-previous-boot", now="2026-01-01T00:00:00+00:00",
        tmux_session="crr-8a1b2c3d",
        claude={"session_id": sid, "sid_source": "injected",
                "started": "2026-01-01T00:00:00+00:00",
                "skip_permissions": False}))

    class FakeTmux:
        def available(self): return True
        def list_sessions(self): return {"crr-8a1b2c3d"}
        def attached_sessions(self): return set()  # parked, not reopened

    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: FakeTmux())
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "[restored]" in out
    assert "[parked]" not in out


def test_revive_passes_the_flag_store_so_close_actually_sticks(tmp_path, monkeypatch):
    # [#58] The reviver can only honour a close flag if the CLI hands it one.
    # Without this wiring the fix is inert in the only place it runs.
    seen = {}

    def spy(*a, **kw):
        seen["flags"] = kw.get("flags")
        from crr.core.reviver import RevivalOutcome
        return RevivalOutcome([], [], [])

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.reviver, "revive_crashed", spy)
    monkeypatch.setattr(cli.tmux, "RealTmux",
                        lambda t: type("T", (), {"available": lambda s: True})())
    assert cli.main(["revive"]) == 0
    assert seen["flags"] is not None, "revive ran without a flag store"


def test_revive_passes_a_tab_spawner_so_a_kicked_session_comes_back_visible(
    tmp_path, monkeypatch
):
    # [#62] The reviver can only open the tab if the CLI hands it a spawner.
    # Without this wiring the fix is inert in the only place it runs.
    seen = {}

    def spy(*a, **kw):
        seen["tab_spawner"] = kw.get("tab_spawner")
        from crr.core.reviver import RevivalOutcome
        return RevivalOutcome([], [], [])

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.reviver, "revive_crashed", spy)
    monkeypatch.setattr(cli, "_tab_spawner", lambda config, **k: ("SPAWNER", True))
    monkeypatch.setattr(cli.tmux, "RealTmux",
                        lambda t: type("T", (), {"available": lambda s: True})())
    assert cli.main(["revive"]) == 0
    assert seen["tab_spawner"] == "SPAWNER"


def test_reachability_matches_a_tmux_revived_session_via_the_live_snapshot(tmp_path, monkeypatch):
    """A tmux-revived session journals the CLAUDE process, not a parent shell.

    `_child_groups` returns `[shell_pgid]` for that shape (#58), so the LIVE
    snapshot matches it. The probe must model that — an earlier version of
    this test faked `{pid: []}`, which cannot tell "claude with no children"
    from "this pid does not exist", and so licensed a match on dead pids.
    """
    from crr.adapters import session_state, state_dir
    from crr.core.journal import JournalStore, new_entry

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    JournalStore(tmp_path).write(new_entry(
        pid=1960, cwd="/home/u/p", host="tmux", shell="bash",
        boot_id="b", now="2026-01-01T00:00:00+00:00", tmux_session="crr-x",
        claude={"session_id": sid, "sid_source": "injected",
                "started": "2026-01-01T00:00:00+00:00",
                "skip_permissions": False}))

    class LiveClaudeLeadsItsGroup:
        def claude_group_pids(self, pids): return {p: [p] for p in pids}

    state = session_state.SessionState(
        pid=1960, bridge_session_id=None, field_present=True,
        status="idle", waiting_for="")
    got = cli._reachability_by_sid(
        JournalStore(tmp_path).scan().entries, LiveClaudeLeadsItsGroup(),
        cfg.Config(), read_session_state=lambda: {sid: state})
    assert got[sid] == ("unreachable", "")


def test_a_dead_pid_state_file_never_licenses_a_claim(tmp_path, monkeypatch):
    """The defect this replaced: after a reboot a revived session's journaled
    pid is dead, but its state file survives with the SAME pid and sid.

    Matching on pid equality alone checked nothing about liveness, so the
    card asserted `reachable` — and "waiting on you" — about a process that
    no longer existed. On this machine that was 13 of 17 cards for the 76
    minutes between boot and the reviver running (adversarial review
    2026-08-10).
    """
    from crr.adapters import session_state, state_dir
    from crr.core.journal import JournalStore, new_entry

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    JournalStore(tmp_path).write(new_entry(
        pid=999999, cwd="/home/u/p", host="tmux", shell="bash",
        boot_id="an-old-boot", now="2026-01-01T00:00:00+00:00", tmux_session="crr-x",
        claude={"session_id": sid, "sid_source": "injected",
                "started": "2026-01-01T00:00:00+00:00",
                "skip_permissions": False}))

    class PidIsGone:
        def claude_group_pids(self, pids): return {p: [] for p in pids}

    stale = session_state.SessionState(
        pid=999999, bridge_session_id="session_from_before_the_reboot",
        field_present=True, status="waiting", waiting_for="permission prompt")
    got = cli._reachability_by_sid(
        JournalStore(tmp_path).scan().entries, PidIsGone(), cfg.Config(),
        read_session_state=lambda: {sid: stale})
    # The invariant is "no positive claim and no leaked state", not a
    # particular container shape: an explicit `unknown` is as honest as an
    # absent key, and `assemble_sessions` reads both the same way.
    assert got[sid] == ("unknown", ""), \
        "a dead pid's stale file must license neither a verdict nor waiting_for"


# --- shim-side conflict block (#48) ---------------------------------------
#
# The card warning reports a second agent after it exists. This refuses to
# create one. Only the explicit-sid resume path can be checked — a fresh
# launch gets a brand-new sid (no conflict possible) and `--continue`
# resolves its sid inside claude, after the point crr could intervene.

def _seed_conflict(tmp_path, sid):
    store = JournalStore(tmp_path)
    for pid in (1687, 1957):
        store.write(new_entry(pid=pid, cwd="/p", host="tmux", shell="fish",
                              boot_id="B", now=_NOW_STR,
                              claude={"session_id": sid, "sid_source": "injected",
                                      "started": _NOW_STR,
                                      "skip_permissions": False}))
    return store


_NOW_STR = "2026-08-10T00:00:00Z"
_CSID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_conflict_check_is_silent_and_clear_when_nothing_else_owns_the_sid(
    tmp_path, monkeypatch, capsys
):
    # ONE existing owner is already a conflict — the shim is about to add a
    # second. "Clear to launch" means nothing is running on this sid at all,
    # which is the normal case: a crashed conversation being resumed.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _seed_conflict(tmp_path, _CSID)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        lambda t: type("P", (), {"claude_group_pids":
                                                 lambda s, pids: {1687: [], 1957: []}})())
    assert cli.main(["conflict-check", "--sid", _CSID]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_one_existing_owner_already_blocks(tmp_path, monkeypatch, capsys):
    # The case that matters most: you resume from a second terminal a
    # conversation the reviver already has parked.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _seed_conflict(tmp_path, _CSID)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        lambda t: type("P", (), {"claude_group_pids":
                                                 lambda s, pids: {1957: [1957]}})())
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["conflict-check", "--sid", _CSID]) != 0


def test_conflict_check_aborts_without_a_tty_rather_than_creating_a_second_agent(
    tmp_path, monkeypatch, capsys
):
    # Unattended must never be the path that starts the duplicate.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _seed_conflict(tmp_path, _CSID)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        lambda t: type("P", (), {"claude_group_pids":
                                                 lambda s, pids: {1687: [11], 1957: [1957]}})())
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    rc = cli.main(["conflict-check", "--sid", _CSID])
    assert rc != 0
    assert "already live" in capsys.readouterr().err.lower()


def test_conflict_check_kills_the_others_when_the_user_chooses_to(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _seed_conflict(tmp_path, _CSID)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        lambda t: type("P", (), {"claude_group_pids":
                                                 lambda s, pids: {1687: [11], 1957: [1957]}})())
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "k")
    closed = []
    monkeypatch.setattr(cli.ops, "close",
                        lambda *a, **k: closed.append(a[5]) or cli.ops.OpResult(True, "closed"))
    assert cli.main(["conflict-check", "--sid", _CSID]) == 0
    assert sorted(closed) == [1687, 1957]


def test_conflict_check_aborts_when_the_user_declines(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    _seed_conflict(tmp_path, _CSID)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        lambda t: type("P", (), {"claude_group_pids":
                                                 lambda s, pids: {1687: [11], 1957: [1957]}})())
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "a")
    closed = []
    monkeypatch.setattr(cli.ops, "close", lambda *a, **k: closed.append(1))
    assert cli.main(["conflict-check", "--sid", _CSID]) != 0
    assert closed == [], "aborting must not kill anything"


# --- conflict-check --cwd: the `--continue` half of the guard (#68) -------
#
# `--continue` resolves its conversation inside claude, after the shim has
# handed off, so there is no sid to check. crr predicts it the same way
# `claude-resume` already does on this exact path — newest transcript in
# the cwd — and checks that. A prediction can be wrong, which is why the
# refusal names the sid and says where it came from, and why empty input
# still aborts: a bad guess costs a keystroke, never a session.

def _probe_owning(pids_by_owner):
    return lambda t: type("P", (), {"claude_group_pids":
                                    lambda s, pids: pids_by_owner})()


def test_conflict_check_by_cwd_predicts_the_sid_continue_would_resume(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _seed_conflict(tmp_path / "state", _CSID)
    # Two transcripts; the newest is the live one, and it is the one
    # `--continue` picks. The older must not be what gets checked.
    _write_transcript_file(tmp_path / "home", "/p",
                           "11111111-aaaa-4aaa-8aaa-111111111111", mtime=1000)
    _write_transcript_file(tmp_path / "home", "/p", _CSID, mtime=5000)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        _probe_owning({1687: [11], 1957: [1957]}))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    rc = cli.main(["conflict-check", "--cwd", "/p"])
    assert rc != 0, "a live conversation in this cwd must block --continue"
    err = capsys.readouterr().err
    assert _CSID[:8] in err
    # The user has to be able to spot a wrong prediction before answering,
    # so the message says the sid was derived, not observed.
    assert "newest" in err.lower()


def test_conflict_check_by_cwd_is_clear_when_the_newest_is_not_live(
    tmp_path, monkeypatch, capsys
):
    # An older conversation being live is not this launch's problem:
    # `--continue` is not going to resume it.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _seed_conflict(tmp_path / "state", _CSID)
    newest = "22222222-bbbb-4bbb-8bbb-222222222222"
    _write_transcript_file(tmp_path / "home", "/p", _CSID, mtime=1000)
    _write_transcript_file(tmp_path / "home", "/p", newest, mtime=5000)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        _probe_owning({1687: [11], 1957: [1957]}))
    assert cli.main(["conflict-check", "--cwd", "/p"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_conflict_check_by_cwd_is_clear_when_there_is_nothing_to_predict(
    tmp_path, monkeypatch, capsys
):
    # No transcripts here: `--continue` has nothing to resume, so there is
    # nothing to conflict with. Never block on an absence.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _seed_conflict(tmp_path / "state", _CSID)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        _probe_owning({1687: [11], 1957: [1957]}))
    assert cli.main(["conflict-check", "--cwd", "/nowhere"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_conflict_check_by_cwd_forces_the_same_choice_as_an_explicit_sid(
    tmp_path, monkeypatch, capsys
):
    # The whole point of #68: --continue is not a softer warning path. Same
    # prompt, same kill, no "carry on anyway".
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _seed_conflict(tmp_path / "state", _CSID)
    _write_transcript_file(tmp_path / "home", "/p", _CSID, mtime=5000)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        _probe_owning({1687: [11], 1957: [1957]}))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "k")
    closed = []
    monkeypatch.setattr(cli.ops, "close",
                        lambda *a, **k: closed.append(a[-1]) or cli.ops.OpResult(True, "closed"))
    assert cli.main(["conflict-check", "--cwd", "/p"]) == 0
    assert closed == [1687, 1957]


def test_conflict_check_by_cwd_aborts_on_empty_input_so_a_bad_guess_is_cheap(
    tmp_path, monkeypatch, capsys
):
    # The load-bearing safety property for a PREDICTED sid: if crr guessed
    # the wrong conversation, hitting Enter must leave everything alone.
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    set_home(monkeypatch, str(tmp_path / "home"))
    _seed_conflict(tmp_path / "state", _CSID)
    _write_transcript_file(tmp_path / "home", "/p", _CSID, mtime=5000)
    monkeypatch.setattr(cli.process_probe, "PsProcessProbe",
                        _probe_owning({1687: [11], 1957: [1957]}))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    closed = []
    monkeypatch.setattr(cli.ops, "close", lambda *a, **k: closed.append(1))
    assert cli.main(["conflict-check", "--cwd", "/p"]) != 0
    assert closed == [], "a wrong prediction must never cost a session"


def test_conflict_check_needs_something_to_check(capsys):
    # Neither --sid nor --cwd: refuse explicitly rather than silently
    # returning "clear to launch", which would disable the guard.
    rc = cli.main(["conflict-check"])
    assert rc != 0
    assert "--sid" in capsys.readouterr().err


def test_qr_prints_code_and_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    class _FakeTS:
        def __init__(self, *_a):
            pass

        def status(self):
            return {"Self": {"DNSName": "lovelace.tail3af2d9.ts.net."}}

        def serve_status(self):
            return {"TCP": {"443": {"HTTPS": True}}}

    monkeypatch.setattr(cli.tailscale, "RealTailscale", _FakeTS)
    rc = cli.main(["qr"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "https://lovelace.tail3af2d9.ts.net/" in out
    # A QR was rendered above the URL, not just the URL line on its own.
    assert out.count("\n") > 1


def test_qr_degrades_with_hint_when_serve_not_live(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)

    class _FakeTS:
        def __init__(self, *_a):
            pass

        def status(self):
            return {"Self": {"DNSName": "lovelace.tail3af2d9.ts.net."}}

        def serve_status(self):
            return None  # serve not configured

    monkeypatch.setattr(cli.tailscale, "RealTailscale", _FakeTS)
    rc = cli.main(["qr"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tailscale serve" in out           # the hint
    assert "127.0.0.1" in out or "loopback" in out
