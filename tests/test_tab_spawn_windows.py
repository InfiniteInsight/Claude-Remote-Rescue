"""Windows Terminal (wt.exe) tab-spawn adapter tests (Phase 4, WSL host).

crr runs inside WSL (a Linux userland) and reaches the Windows side through
``wt.exe`` / ``wsl.exe``. The command builder is pure and asserted
structurally; the spawner wiring is captured via a monkeypatched
``subprocess.run`` so nothing is launched. NONE of this is verifiable from
Linux CI — only the builder/parse logic is; the real wt.exe integration is
author-verified on Windows (task #8's Windows replay).
"""

import subprocess

import pytest

from crr.adapters import tab_spawn_windows as tsw
from crr.core import tab_health
from crr.core.ports import TabSpawnTimeout


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
    monkeypatch.setattr(tsw, "wt_probe", lambda path, timeout: True)
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


def test_available_is_false_when_wt_exe_probe_fails(monkeypatch):
    """A broken App Execution Alias or tmux/systemd context: wt.exe exists
    and interop is registered but the binary exits non-zero. available()
    must return False so callers refuse the operation cleanly."""
    monkeypatch.setattr(tsw, "wt_path", lambda: "/mnt/c/fake/wt.exe")
    monkeypatch.setattr(tsw, "interop_registered", lambda: True)
    monkeypatch.setattr(tsw, "wt_probe", lambda path, timeout: False)
    assert tsw.WindowsTerminalSpawner(5).available() is False


def test_available_is_true_when_wt_exe_probe_succeeds(monkeypatch):
    monkeypatch.setattr(tsw, "wt_path", lambda: "/mnt/c/fake/wt.exe")
    monkeypatch.setattr(tsw, "interop_registered", lambda: True)
    monkeypatch.setattr(tsw, "wt_probe", lambda path, timeout: True)
    assert tsw.WindowsTerminalSpawner(5).available() is True


def test_available_probe_false_skips_the_window_opening_probe(monkeypatch):
    """probe=False (best-effort reopen / rescue re-home) must NOT call
    wt_probe — that is the only step that opens a GUI help window
    [/exit revival 2026-08-25]. The windowless checks still gate it."""
    monkeypatch.setattr(tsw, "wt_path", lambda: "/mnt/c/fake/wt.exe")
    monkeypatch.setattr(tsw, "interop_registered", lambda: True)

    def _boom(path, timeout):
        raise AssertionError("wt_probe must not run when probe=False")

    monkeypatch.setattr(tsw, "wt_probe", _boom)
    assert tsw.WindowsTerminalSpawner(5).available(probe=False) is True


def test_available_probe_false_still_requires_wt_and_interop(monkeypatch):
    # Skipping the probe does not skip the windowless prerequisites: a
    # missing wt.exe or unregistered interop is still False.
    monkeypatch.setattr(tsw, "wt_path", lambda: None)
    monkeypatch.setattr(tsw, "interop_registered", lambda: True)
    monkeypatch.setattr(tsw, "wt_probe", lambda path, timeout: True)
    assert tsw.WindowsTerminalSpawner(5).available(probe=False) is False


def test_available_uses_the_resolved_path_not_only_path(tmp_path, monkeypatch):
    # A stale service PATH must not read as "Windows Terminal is missing".
    found = tmp_path / "mnt/c/Users/Someone/AppData/Local/Microsoft/WindowsApps/wt.exe"
    found.parent.mkdir(parents=True)
    found.write_text("")
    monkeypatch.setattr(tsw.shutil, "which", lambda b: None)
    monkeypatch.setattr(tsw, "MNT_ROOT", tmp_path / "mnt")
    monkeypatch.setattr(tsw, "interop_registered", lambda: True)
    monkeypatch.setattr(tsw, "wt_probe", lambda path, timeout: True)
    assert tsw.WindowsTerminalSpawner(30).available() is True


def test_command_uses_the_resolved_wt_path(monkeypatch):
    monkeypatch.setattr(tsw, "wt_path", lambda: "/mnt/c/Users/Other/wt.exe")
    cmd = tsw.wt_command(_ARGV)
    assert cmd[0] == "/mnt/c/Users/Other/wt.exe"


# --- alternate launcher tiers (wt.exe alias fallthrough) -------------------


def test_aumid_command_uses_the_stable_package_family_name():
    cmd = tsw.aumid_command(["tmux", "attach"], distro="Ubuntu-24.04")
    joined = " ".join(cmd)
    assert cmd[0] == "powershell.exe"
    assert "-NoProfile" in cmd
    # Family name, NOT a versioned package full name — stable across upgrades.
    assert "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App" in joined
    assert "1.24" not in joined


def test_aumid_command_passes_new_tab_and_the_wsl_argv():
    cmd = tsw.aumid_command(["tmux", "attach", "-t", "crr-abc"],
                            distro="Ubuntu-24.04")
    joined = " ".join(cmd)
    assert "'new-tab'" in joined
    assert "'wsl.exe'" in joined
    assert "'--distribution','Ubuntu-24.04'" in joined
    assert "'-e','tmux','attach','-t','crr-abc'" in joined


def test_aumid_command_includes_profile_and_cwd_when_given():
    cmd = tsw.aumid_command(["tmux"], cwd="/home/u/p", profile="crr")
    joined = " ".join(cmd)
    assert "'-p','crr'" in joined
    assert "'-d','/home/u/p'" in joined


def test_aumid_command_omits_profile_and_cwd_when_absent():
    joined = " ".join(tsw.aumid_command(["tmux"]))
    assert "'-p'" not in joined
    assert "'-d'" not in joined
    assert "'--distribution'" not in joined


def test_console_command_launches_wsl_without_windows_terminal():
    cmd = tsw.console_command(["tmux", "attach"], distro="Ubuntu-24.04")
    joined = " ".join(cmd)
    assert cmd[0] == "powershell.exe"
    assert "Start-Process wsl.exe" in joined
    assert "'--distribution','Ubuntu-24.04'" in joined
    assert "'-e','tmux','attach'" in joined
    # The console fallback must not reference Windows Terminal at all.
    assert "WindowsTerminal" not in joined
    assert "new-tab" not in joined


# --- Finding 2: cheap insurance against a non-terminating Start-Process
# error going unnoticed. Measured on a real host: an unresolvable AUMID
# exits 1 by default, so tier2->tier3 fallthrough already works without
# this — it is added purely as insurance for other failure modes, not a
# restructuring of the commands.

def test_aumid_command_sets_error_action_preference_to_stop():
    cmd = tsw.aumid_command(["tmux"])
    command_str = cmd[-1]
    assert command_str.startswith("$ErrorActionPreference='Stop'; ")
    assert command_str.index("$ErrorActionPreference") < command_str.index("Start-Process")


def test_console_command_sets_error_action_preference_to_stop():
    cmd = tsw.console_command(["tmux"])
    command_str = cmd[-1]
    assert command_str.startswith("$ErrorActionPreference='Stop'; ")
    assert command_str.index("$ErrorActionPreference") < command_str.index("Start-Process")


def test_ps_quoting_escapes_embedded_single_quotes():
    joined = " ".join(tsw.console_command(["echo", "it's"]))
    # PowerShell escapes a single quote by doubling it.
    assert "'it''s'" in joined


def test_ps_quoting_handles_paths_with_spaces():
    """A value containing a space must reach the target process as ONE
    argument. `_ps_quote` correctly single-quotes for the PowerShell
    parse, but Start-Process's -ArgumentList joins items with plain spaces
    and does NOT re-quote them when building the target's command line —
    measured on a real host: -ArgumentList '-e','touch','/tmp/space
    test/marker' created a stray file at '/tmp/space' instead of the
    intended path, and exited 0 (silently wrong). Embedding literal double
    quotes inside the PowerShell single-quoted item survives the join and
    is parsed back as ONE argument by the target's CreateProcess-style
    argv splitter — measured to work correctly on a real host."""
    joined = " ".join(tsw.aumid_command(["tmux"], cwd="/home/u/my proj"))
    assert "'\"/home/u/my proj\"'" in joined


def test_ps_quoting_composes_space_and_single_quote_escaping():
    # The single-quote doubling PowerShell needs and the double-quote
    # wrapping the target process needs must compose, not clobber each
    # other, for a value carrying both a space and an embedded quote.
    joined = " ".join(tsw.console_command(["touch", "it's a test/marker"]))
    assert "'\"it''s a test/marker\"'" in joined


def test_ps_quote_of_an_empty_string_does_not_vanish():
    # Finding 10: Start-Process's plain-space join silently drops a bare
    # '' item, shifting every argument after it. No caller passes one
    # today, but the same embedded-double-quote wrapping keeps an empty
    # value a real (empty) argument instead of disappearing.
    assert tsw._ps_quote("") == "'\"\"'"


# --- launcher tier fallthrough ----------------------------------------------


def _spawner():
    return tsw.WindowsTerminalSpawner(timeout_seconds=5, distro="Ubuntu-24.04")


def _runner(fail_first: int):
    """Fake subprocess.run: raise CalledProcessError for the first N calls."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) <= fail_first:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    run.calls = calls
    return run


def test_tier1_success_records_wt_and_stops(monkeypatch):
    runner = _runner(fail_first=0)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    sp.open_tab(["tmux", "attach"])
    assert len(runner.calls) == 1
    assert sp.last_tier == tab_health.TIER_WT
    assert sp.last_confirmed is True


def test_tier1_failure_falls_through_to_aumid(monkeypatch):
    runner = _runner(fail_first=1)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    sp.open_tab(["tmux", "attach"])
    assert len(runner.calls) == 2
    assert "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App" in " ".join(runner.calls[1])
    assert sp.last_tier == tab_health.TIER_AUMID
    # Start-Process is fire-and-forget: launched, not confirmed.
    assert sp.last_confirmed is False


def test_tier2_failure_falls_through_to_console(monkeypatch):
    runner = _runner(fail_first=2)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    sp.open_tab(["tmux", "attach"])
    assert len(runner.calls) == 3
    assert "Start-Process wsl.exe" in " ".join(runner.calls[2])
    assert sp.last_tier == tab_health.TIER_CONSOLE
    assert sp.last_confirmed is False


def test_all_tiers_failing_raises_and_records_none(monkeypatch):
    runner = _runner(fail_first=3)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    with pytest.raises(subprocess.CalledProcessError):
        sp.open_tab(["tmux", "attach"])
    assert len(runner.calls) == 3
    assert sp.last_tier == tab_health.TIER_NONE


def test_all_tiers_failing_raises_the_underlying_error_not_a_typeerror(monkeypatch):
    """Finding 4: the final raise must be a real error whether or not the
    interpreter strips `assert` (python -O). Guard against a bare `raise
    None` masquerading as a TypeError by asserting the raised exception IS
    the underlying subprocess error, with its original message intact."""
    runner = _runner(fail_first=3)
    monkeypatch.setattr(tsw.subprocess, "run", runner)
    sp = _spawner()
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        sp.open_tab(["tmux", "attach"])
    assert excinfo.type is subprocess.CalledProcessError
    assert excinfo.value.returncode == 1


# --- Finding 5: tier commands are built lazily, only when attempted --------


def test_later_tier_commands_are_not_built_when_an_earlier_tier_succeeds(monkeypatch):
    calls = {"wt": 0, "aumid": 0, "console": 0}
    real_wt_command = tsw.wt_command

    def spy_wt(*a, **kw):
        calls["wt"] += 1
        return real_wt_command(*a, **kw)

    def spy_aumid(*a, **kw):
        calls["aumid"] += 1
        raise AssertionError("aumid_command must not be built when tier 1 succeeds")

    def spy_console(*a, **kw):
        calls["console"] += 1
        raise AssertionError("console_command must not be built when tier 1 succeeds")

    monkeypatch.setattr(tsw, "wt_command", spy_wt)
    monkeypatch.setattr(tsw, "aumid_command", spy_aumid)
    monkeypatch.setattr(tsw, "console_command", spy_console)
    monkeypatch.setattr(tsw, "wt_path", lambda: "wt.exe")
    monkeypatch.setattr(tsw.subprocess, "run",
                         lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    tsw.WindowsTerminalSpawner(5).open_tab(["tmux", "attach"])
    assert calls == {"wt": 1, "aumid": 0, "console": 0}


def test_tier2_command_is_built_only_after_tier1_is_attempted(monkeypatch):
    order = []
    monkeypatch.setattr(tsw, "wt_path", lambda: "wt.exe")

    real_aumid_command = tsw.aumid_command

    def spy_aumid(*a, **kw):
        order.append("aumid_built")
        return real_aumid_command(*a, **kw)

    monkeypatch.setattr(tsw, "aumid_command", spy_aumid)

    def run(cmd, **kwargs):
        order.append(f"run:{cmd[0]}")
        if len(order) == 1:  # the tier-1 attempt (built eagerly before the call)
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tsw.subprocess, "run", run)
    tsw.WindowsTerminalSpawner(5).open_tab(["tmux", "attach"])
    # aumid_command must be built AFTER the tier-1 attempt fails, not before.
    assert order.index("aumid_built") > order.index("run:wt.exe")


# --- Finding 8: a reused spawner must not launder a stale tier -------------
#
# rescue-check loops one spawner over N entries; so does the reviver
# (Task 3 wiring). If an exception escapes open_tab before the loop
# assigns last_tier (e.g. a builder itself blows up), the caller's
# `except Exception` would otherwise record the PREVIOUS spawn's tier —
# including a timed-out one, quietly defeating the never-record-on-timeout
# invariant.


def test_a_second_failing_spawn_does_not_report_the_first_spawns_tier(monkeypatch):
    monkeypatch.setattr(tsw, "wt_path", lambda: "wt.exe")
    monkeypatch.setattr(tsw.subprocess, "run",
                         lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    sp = _spawner()
    sp.open_tab(["tmux", "attach"])
    assert sp.last_tier == tab_health.TIER_WT
    assert sp.last_confirmed is True

    def boom_wt_command(*a, **kw):
        raise RuntimeError("boom before any tier is attempted")

    monkeypatch.setattr(tsw, "wt_command", boom_wt_command)
    with pytest.raises(RuntimeError):
        sp.open_tab(["tmux", "attach"])
    # Reset at the top of open_tab, not laundered from the first spawn.
    assert sp.last_tier is None
    assert sp.last_confirmed is False


def test_a_timeout_does_not_fall_through(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(tsw.subprocess, "run", run)
    sp = _spawner()
    with pytest.raises(TabSpawnTimeout):
        sp.open_tab(["tmux", "attach"])
    # A cold Windows Terminal may still open the tab (#53). Trying the next
    # tier would risk a second window; exactly one attempt must be made.
    assert len(calls) == 1
