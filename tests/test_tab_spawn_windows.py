"""Windows Terminal (wt.exe) tab-spawn adapter tests (Phase 4, WSL host).

crr runs inside WSL (a Linux userland) and reaches the Windows side through
``wt.exe`` / ``wsl.exe``. The command builder is pure and asserted
structurally; the spawner wiring is captured via a monkeypatched
``subprocess.run`` so nothing is launched. NONE of this is verifiable from
Linux CI — only the builder/parse logic is; the real wt.exe integration is
author-verified on Windows (task #8's Windows replay).
"""

from crr.adapters import tab_spawn_windows as tsw


_ARGV = ["tmux", "attach", "-t", "crr-abc12345"]


def test_wt_command_runs_argv_in_wsl_via_new_tab():
    # From WSL, a visible tab is a Windows Terminal tab that re-enters this
    # distro (wsl.exe -e) and runs the word-form argv.
    cmd = tsw.wt_command(_ARGV)
    assert cmd == ["wt.exe", "new-tab", "wsl.exe", "-e", *_ARGV]


def test_wt_command_threads_profile_startdir_and_distro():
    cmd = tsw.wt_command(_ARGV, cwd="/home/u/p", profile="Ubuntu", distro="Ubuntu-22.04")
    assert cmd == [
        "wt.exe", "new-tab", "-p", "Ubuntu", "-d", "/home/u/p",
        "wsl.exe", "--distribution", "Ubuntu-22.04", "-e", *_ARGV,
    ]


def test_is_wsl_reads_proc_version(tmp_path):
    micro = tmp_path / "version_wsl"
    micro.write_text("Linux version 5.15.0-microsoft-standard-WSL2 ...", encoding="utf-8")
    assert tsw.is_wsl(str(micro)) is True

    native = tmp_path / "version_native"
    native.write_text("Linux version 6.8.0-generic ...", encoding="utf-8")
    assert tsw.is_wsl(str(native)) is False

    assert tsw.is_wsl(str(tmp_path / "missing")) is False  # absent -> not WSL


def test_spawner_open_tab_runs_the_built_command(monkeypatch):
    calls = []
    monkeypatch.setattr(tsw.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    tsw.WindowsTerminalSpawner(5, profile="Ubuntu").open_tab(_ARGV)
    assert calls == [["wt.exe", "new-tab", "-p", "Ubuntu", "wsl.exe", "-e", *_ARGV]]


def test_available_reflects_which(monkeypatch):
    monkeypatch.setattr(tsw.shutil, "which", lambda b: "/mnt/c/.../wt.exe")
    assert tsw.WindowsTerminalSpawner(5).available() is True
    monkeypatch.setattr(tsw.shutil, "which", lambda b: None)
    assert tsw.WindowsTerminalSpawner(5).available() is False
