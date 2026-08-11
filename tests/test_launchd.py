"""launchd agents adapter tests (macOS Phase 2).

The launchd analogue of ``test_systemd.py``. Plists are built with
``plistlib`` (stdlib), so the strongest assertions parse the generated
XML back into a dict and check *structure* — these run on every CI
runner, not just macOS. A gated ``plutil -lint`` check is the macOS
real-tool gate (the analogue of ``systemd-analyze verify`` / ``node
--check``); note it only validates well-formedness, so the ``plistlib``
asserts carry the semantic weight.
"""

import os
import plistlib
import shutil
import subprocess

import pytest

from crr.adapters import launchd


def _parse(content: str) -> dict:
    return plistlib.loads(content.encode("utf-8"))


def test_revive_agent_runs_crr_revive_on_an_interval():
    plist = _parse(launchd.revive_agent_plist(
        crr_bin="/opt/crr/bin/crr",
        path="/opt/crr/bin:/usr/bin:/bin",
        interval_seconds=30,
    ))
    assert plist["Label"] == launchd.REVIVE_LABEL
    assert plist["ProgramArguments"] == ["/opt/crr/bin/crr", "revive"]
    assert plist["StartInterval"] == 30
    assert plist["RunAtLoad"] is True
    # [lesson: interop PATH] — a launchd agent gets a minimal default PATH
    # that excludes Homebrew, so the resolved PATH must be baked in or every
    # revival execs into a missing `claude`/`tmux` and dies instantly.
    assert plist["EnvironmentVariables"]["PATH"] == "/opt/crr/bin:/usr/bin:/bin"


def test_web_agent_runs_crr_web_and_keeps_alive():
    plist = _parse(launchd.web_agent_plist(
        crr_bin="/opt/crr/bin/crr",
        path="/opt/crr/bin:/usr/bin",
        port=8377,
    ))
    assert plist["Label"] == launchd.WEB_LABEL
    assert plist["ProgramArguments"] == ["/opt/crr/bin/crr", "web", "--port", "8377"]
    # KeepAlive + RunAtLoad keep the dashboard up while logged in and bring
    # it back at login — NOT headless-across-reboot like Linux linger (user
    # agents have no linger equivalent; that would need a LaunchDaemon,
    # which DESIGN.md deliberately did not choose).
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    assert plist["EnvironmentVariables"]["PATH"] == "/opt/crr/bin:/usr/bin"


def test_generated_plists_have_the_plist_doctype_and_are_wellformed():
    # plistlib emits the Apple DOCTYPE + declaration; a smoke check that we
    # produce a real plist document, not a bare dict dump.
    content = launchd.revive_agent_plist(crr_bin="/c", path="/p", interval_seconds=5)
    assert content.startswith("<?xml")
    assert "<!DOCTYPE plist" in content


def test_mac_path_baseline_includes_homebrew_dirs():
    # Both Homebrew prefixes are in the baseline so a Mac revival resolves
    # `claude`/`tmux` regardless of Apple-Silicon vs Intel install layout.
    assert "/opt/homebrew/bin" in launchd._MAC_PATH_DIRS   # Apple Silicon
    assert "/usr/local/bin" in launchd._MAC_PATH_DIRS       # Intel


@pytest.mark.skipif(
    os.name == "nt",
    reason="asserts on POSIX path literals; os.path.abspath composes "
           "them under ntpath here, so they acquire a drive letter "
           "and the assertion measures path semantics rather than "
           "crr. The unit this PATH goes into targets Linux/macOS",
)
def test_resolve_service_path_includes_crr_dir_and_reports_missing(monkeypatch):
    # crr's own dir always leads the PATH; unresolved service binaries are
    # reported (a silent missing `claude` would kill every revival on exec).
    monkeypatch.setattr(launchd.shutil, "which", lambda name: None)
    path, missing = launchd.resolve_service_path("/opt/crr/bin/crr")
    assert path.split(":")[0] == "/opt/crr/bin"
    assert set(missing) == set(launchd.SERVICE_BINARIES)


def test_agent_dir_is_library_launchagents():
    from pathlib import Path
    assert launchd.agent_dir(Path("/Users/u")) == Path("/Users/u/Library/LaunchAgents")


def test_write_agents_writes_all_named_files(tmp_path):
    paths = launchd.write_agents(tmp_path, {
        launchd.REVIVE_PLIST: "REVIVE", launchd.WEB_PLIST: "WEB",
    })
    names = {p.name for p in paths}
    assert names == {launchd.REVIVE_PLIST, launchd.WEB_PLIST}
    assert (tmp_path / launchd.WEB_PLIST).read_text() == "WEB"


def test_enable_commands_load_both_agents(tmp_path):
    cmds = launchd.enable_commands(tmp_path)
    revive = str(tmp_path / launchd.REVIVE_PLIST)
    web = str(tmp_path / launchd.WEB_PLIST)
    assert ["launchctl", "load", "-w", revive] in cmds
    assert ["launchctl", "load", "-w", web] in cmds


def test_disable_commands_unload_both_agents(tmp_path):
    cmds = launchd.disable_commands(tmp_path)
    assert cmds == [
        ["launchctl", "unload", "-w", str(tmp_path / launchd.REVIVE_PLIST)],
        ["launchctl", "unload", "-w", str(tmp_path / launchd.WEB_PLIST)],
    ]


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available (non-macOS)")
def test_generated_plists_pass_plutil_lint(tmp_path):
    # macOS real-tool gate: plutil must accept the generated plists. Uses a
    # binary that exists as the stand-in crr_bin; plutil -lint checks
    # well-formedness only, so a runner lacking tmux/claude still passes —
    # those just land in `missing`, which is the behavior we want.
    crr_bin = shutil.which("true")
    path, _ = launchd.resolve_service_path(crr_bin)
    agents = {
        launchd.REVIVE_PLIST: launchd.revive_agent_plist(crr_bin=crr_bin, path=path, interval_seconds=30),
        launchd.WEB_PLIST: launchd.web_agent_plist(crr_bin=crr_bin, path=path, port=8377),
    }
    for p in launchd.write_agents(tmp_path, agents):
        result = subprocess.run(["plutil", "-lint", str(p)], capture_output=True, text=True)
        assert result.returncode == 0, f"{p.name}: {result.stdout}{result.stderr}"
