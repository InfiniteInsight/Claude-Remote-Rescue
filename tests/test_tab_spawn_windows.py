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


def test_wt_command_runs_argv_in_wsl_via_new_tab(monkeypatch):
    monkeypatch.setattr(tsw, "wt_path", lambda: "wt.exe")
    # From WSL, a visible tab is a Windows Terminal tab that re-enters this
    # distro (wsl.exe -e) and runs the word-form argv.
    cmd = tsw.wt_command(_ARGV)
    assert cmd == ["wt.exe", "new-tab", "wsl.exe", "-e", *_ARGV]


def test_wt_command_threads_profile_startdir_and_distro(monkeypatch):
    monkeypatch.setattr(tsw, "wt_path", lambda: "wt.exe")
    cmd = tsw.wt_command(_ARGV, cwd="/home/u/p", profile="Ubuntu", distro="Ubuntu-22.04")
    assert cmd == [
        "wt.exe", "new-tab", "-p", "Ubuntu", "-d", "/home/u/p",
        "wsl.exe", "--distribution", "Ubuntu-22.04", "-e", *_ARGV,
    ]


def test_spawner_open_tab_runs_the_built_command(monkeypatch):
    monkeypatch.setattr(tsw, "wt_path", lambda: "wt.exe")
    calls = []
    monkeypatch.setattr(tsw.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    tsw.WindowsTerminalSpawner(5, profile="Ubuntu").open_tab(_ARGV)
    assert calls == [["wt.exe", "new-tab", "-p", "Ubuntu", "wsl.exe", "-e", *_ARGV]]


def test_available_reflects_which(monkeypatch, tmp_path):
    monkeypatch.setattr(tsw.shutil, "which", lambda b: "/mnt/c/.../wt.exe")
    monkeypatch.setattr(tsw, "interop_registered", lambda: True)
    assert tsw.WindowsTerminalSpawner(5).available() is True
    # Neither on PATH nor findable under /mnt -> genuinely absent.
    monkeypatch.setattr(tsw.shutil, "which", lambda b: None)
    monkeypatch.setattr(tsw, "MNT_ROOT", tmp_path / "no-windows-here")
    assert tsw.WindowsTerminalSpawner(5).available() is False


# --- interop_registered ([live bug, 2026-08-09]) --------------------------
#
# shutil.which can never fail on DrvFs: every file under /mnt/c looks
# executable, so wt.exe is "found" even when the kernel cannot exec it. The
# handler that makes a PE binary runnable is binfmt_misc's WSLInterop entry,
# and a systemd remount of /proc/sys/fs/binfmt_misc replaces the instance WSL
# registered into at boot — leaving wt.exe on PATH and ENOEXEC on exec.

def test_interop_registered_true_when_a_handler_is_enabled(tmp_path):
    d = tmp_path / "binfmt_misc"
    d.mkdir()
    (d / "WSLInterop").write_text("enabled\ninterpreter /init\n")
    assert tsw.interop_registered(d) is True


def test_interop_registered_accepts_the_late_handler(tmp_path):
    # Newer WSL images register WSLInterop-late instead; either name counts.
    d = tmp_path / "binfmt_misc"
    d.mkdir()
    (d / "WSLInterop-late").write_text("enabled\ninterpreter /init\n")
    assert tsw.interop_registered(d) is True


def test_interop_registered_false_when_the_handler_is_disabled(tmp_path):
    d = tmp_path / "binfmt_misc"
    d.mkdir()
    (d / "WSLInterop").write_text("disabled\ninterpreter /init\n")
    assert tsw.interop_registered(d) is False


def test_interop_registered_false_when_the_fs_was_remounted_empty(tmp_path):
    # The live failure: binfmt_misc mounted and enabled, but WSL's own
    # registration is gone — only `register` and `status` remain.
    d = tmp_path / "binfmt_misc"
    d.mkdir()
    (d / "register").write_text("")
    (d / "status").write_text("enabled\n")
    assert tsw.interop_registered(d) is False


def test_interop_registered_false_when_binfmt_misc_is_absent(tmp_path):
    assert tsw.interop_registered(tmp_path / "nope") is False


def test_available_is_false_when_interop_is_unregistered(monkeypatch):
    # wt.exe resolves but cannot exec — report no spawner so reopen degrades
    # to the honest "attach with: tmux attach -t ..." rather than an errno.
    monkeypatch.setattr(tsw.shutil, "which", lambda b: "/mnt/c/.../wt.exe")
    monkeypatch.setattr(tsw, "interop_registered", lambda: False)
    assert tsw.WindowsTerminalSpawner(5).available() is False


# --- cold start (#53) ------------------------------------------------------
#
# A warm Windows Terminal answers in milliseconds; a cold one can take longer
# than the 5s interop budget meant for ps/tmux probes. A timeout is NOT
# evidence the tab failed — it usually means the opposite — so the adapter
# reports it as its own thing instead of letting it look like a crash.

def test_spawner_raises_tab_spawn_timeout_not_a_generic_error(monkeypatch):
    import subprocess as sp
    from crr.core.ports import TabSpawnTimeout

    def boom(cmd, **kw):
        raise sp.TimeoutExpired(cmd, kw.get("timeout", 30))

    monkeypatch.setattr(tsw.subprocess, "run", boom)
    try:
        tsw.WindowsTerminalSpawner(30).open_tab(_ARGV)
    except TabSpawnTimeout as exc:
        assert exc.seconds == 30
    else:
        raise AssertionError("expected TabSpawnTimeout")


def test_spawner_still_raises_normally_for_a_real_failure(monkeypatch):
    from crr.core.ports import TabSpawnTimeout

    def boom(cmd, **kw):
        raise OSError(8, "Exec format error", "wt.exe")

    monkeypatch.setattr(tsw.subprocess, "run", boom)
    try:
        tsw.WindowsTerminalSpawner(30).open_tab(_ARGV)
    except TabSpawnTimeout:
        raise AssertionError("a hard failure must not masquerade as a timeout")
    except OSError:
        pass


# --- locate wt.exe at call time (#54) -------------------------------------
#
# crr systemd bakes the WindowsApps dir into the service PATH. Move or rename
# the Windows user profile and that snapshot points nowhere, with no signal
# beyond a degraded reopen. Fall back to a direct look under /mnt/*/Users.

def test_wt_path_prefers_what_is_on_path(monkeypatch):
    monkeypatch.setattr(tsw.shutil, "which", lambda b: "/mnt/c/from/PATH/wt.exe")
    assert tsw.wt_path() == "/mnt/c/from/PATH/wt.exe"


def test_wt_path_falls_back_to_a_windowsapps_search(tmp_path, monkeypatch):
    found = tmp_path / "mnt/c/Users/Someone/AppData/Local/Microsoft/WindowsApps/wt.exe"
    found.parent.mkdir(parents=True)
    found.write_text("")
    monkeypatch.setattr(tsw.shutil, "which", lambda b: None)
    monkeypatch.setattr(tsw, "MNT_ROOT", tmp_path / "mnt")
    assert tsw.wt_path() == str(found)


def test_wt_path_is_none_when_it_genuinely_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(tsw.shutil, "which", lambda b: None)
    monkeypatch.setattr(tsw, "MNT_ROOT", tmp_path / "nothing-here")
    assert tsw.wt_path() is None


def test_wt_probe_returns_false_on_nonzero_exit(monkeypatch):
    """A broken App Execution Alias exits 1 with 'Invalid argument' but does
    not raise an OSError. The probe must check the return code."""
    import subprocess as _sp
    monkeypatch.setattr(tsw.subprocess, "run", lambda *a, **kw: _sp.CompletedProcess(a[0], 1))
    assert tsw.wt_probe("/fake/wt.exe", 5) is False


def test_wt_probe_returns_true_on_zero_exit(monkeypatch):
    import subprocess as _sp
    monkeypatch.setattr(tsw.subprocess, "run", lambda *a, **kw: _sp.CompletedProcess(a[0], 0))
    assert tsw.wt_probe("/fake/wt.exe", 5) is True


def test_available_does_not_probe_wt_exe(monkeypatch):
    """available() must NOT run wt.exe — it opens a GUI help window.

    Executability is verified at open_tab() time instead; available() only
    checks that wt.exe exists and the interop handler is registered.
    """
    monkeypatch.setattr(tsw, "wt_path", lambda: "/mnt/c/fake/wt.exe")
    monkeypatch.setattr(tsw, "interop_registered", lambda: True)
    monkeypatch.setattr(tsw, "wt_probe", lambda path, timeout: (_ for _ in ()).throw(
        AssertionError("wt_probe must not be called from available()")))
    assert tsw.WindowsTerminalSpawner(5).available() is True


def test_available_uses_the_resolved_path_not_only_path(tmp_path, monkeypatch):
    # A stale service PATH must not read as "Windows Terminal is missing".
    found = tmp_path / "mnt/c/Users/Someone/AppData/Local/Microsoft/WindowsApps/wt.exe"
    found.parent.mkdir(parents=True)
    found.write_text("")
    monkeypatch.setattr(tsw.shutil, "which", lambda b: None)
    monkeypatch.setattr(tsw, "MNT_ROOT", tmp_path / "mnt")
    monkeypatch.setattr(tsw, "interop_registered", lambda: True)
    assert tsw.WindowsTerminalSpawner(30).available() is True


def test_command_uses_the_resolved_wt_path(monkeypatch):
    monkeypatch.setattr(tsw, "wt_path", lambda: "/mnt/c/Users/Other/wt.exe")
    cmd = tsw.wt_command(_ARGV)
    assert cmd[0] == "/mnt/c/Users/Other/wt.exe"
