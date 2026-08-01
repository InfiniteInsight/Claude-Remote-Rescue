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
    # [Task 7] print mode must still list linger (enable_commands(), not the
    # critical-only split) — pinned at the CLI boundary, not just algebraically
    # in test_systemd.py, so a future cli.py swap to critical_enable_commands()
    # here would be caught.
    assert "loginctl enable-linger" in out
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
        captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(stdout="enabled\n", stderr="")

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


def _human_card(model, duplicate_group=None, sid_source="injected"):
    return {
        "pid": 42, "sid8": "8a1b2c3d", "state": "live", "cwd": "/home/u/proj",
        "model": model, "duplicate_group": duplicate_group, "sid_source": sid_source,
    }


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


class _FakeTmuxUnknown:
    """F16: available() but list_sessions() can't determine liveness."""

    def __init__(self, *a, **k):
        pass

    def available(self):
        return True

    def list_sessions(self):
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
    # Finding 3 (re-audit): same stderr note as `crr rescued` (item 2). The
    # interactive shims redirect this command's stderr to /dev/null on
    # startup, so this stays quiet there; a manual `crr rescue-check` sees
    # it.
    assert "crr rescued: tmux state unknown — rescued sessions may be undercounted" in err
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is False  # nothing claimed


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
    monkeypatch.setattr(cli, "_tab_spawner", lambda config: None)
    monkeypatch.setattr(cli.rescue, "claim_prompt", lambda *a, **k: False)

    rc = cli.main(["rescue-check"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "" and err == ""


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


def test_rescue_check_yes_routes_failure_message_to_stdout(tmp_path, monkeypatch, capsys):
    """All three shims invoke `crr rescue-check 2>/dev/null`, so anything
    written to stderr from this consent path is thrown away — a user who
    just typed 'y' would never see a detmux failure. Both success and
    failure messages from the post-consent loop must land on stdout."""
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    def fake_detmux(store, archive, tmux_spawner, boot, probe, pid, now, tab_spawner=None):
        if pid == 42:
            return SimpleNamespace(ok=True, message=f"crr: #{pid} de-tmuxed")
        return SimpleNamespace(ok=False, message=f"crr: #{pid} de-tmux failed")

    monkeypatch.setattr(cli.ops, "detmux", fake_detmux)

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
    # (decline), not propagate — a shim hook that lets a KeyboardInterrupt
    # escape breaks the "never break the shell" guarantee. The marker was
    # already written by claim_prompt BEFORE this prompt was printed (the
    # winner claims before prompting), so the once-per-boot invariant holds
    # even though the interrupt unwinds past the detmux/decline branch.
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
    monkeypatch.setattr(cli.ops, "detmux", lambda *a, **k: calls.append(1))

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
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config: _FakeTab())

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.select, "select", _boom)

    rc = cli.main(["rescue-check"])
    assert rc == 0
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


def test_shim_output_carries_a_version_stamp(capsys):
    """[audit P7] generated shims stamp the crr + config-defaults versions."""
    assert cli.main(["shim", "bash", "--crr-bin", "/x/crr"]) == 0
    out = capsys.readouterr().out
    assert "generated by crr " in out and "config-defaults v" in out
    assert "@CRR_VERSION@" not in out and "@CRR_DEFAULTS_V@" not in out
