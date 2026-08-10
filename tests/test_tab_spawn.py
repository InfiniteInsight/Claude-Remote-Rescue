"""macOS tab-spawn adapter tests (Phase 2).

The adapter opens a *visible* terminal tab (Terminal.app / iTerm2) via
``osascript``. As with launchd, actually opening a GUI tab cannot run in
CI (no Aqua session, TCC automation prompts) — so the pure AppleScript
*builders* carry all the verification weight, tested two ways:

1. Two-layer escaping round-trips hostile input (argv→shell via
   ``shlex.quote``, shell→AppleScript literal), so nothing can break out.
2. A gated ``osacompile`` syntax check (the real-tool analogue of ``node
   --check`` / ``plutil -lint``); it compiles but does NOT launch the app.

``open_tab``/``available`` are one-line subprocess wrappers; they are
tested by capturing the argv (monkeypatched ``subprocess.run``) so nothing
is ever actually executed — no tab is opened by the test suite.
"""

import shlex
import shutil
import subprocess

import pytest

from crr.adapters import tab_spawn


def _applescript_unescape(s: str) -> str:
    # Reverse of _as_applescript_string: undo the quote escape first, then
    # the backslash escape (opposite order of application).
    return s.replace('\\"', '"').replace("\\\\", "\\")


# --- pure builders / escaping --------------------------------------------

def test_shell_command_needs_no_quoting_for_plain_argv():
    cmd = tab_spawn._shell_command(["tmux", "attach", "-t", "crr-abc12345"], cwd=None)
    assert cmd == "tmux attach -t crr-abc12345"


def test_shell_command_prepends_cd_when_cwd_given():
    cmd = tab_spawn._shell_command(["claude"], cwd="/home/u/p 1")
    assert cmd == "cd '/home/u/p 1' && claude"


def test_as_applescript_string_escapes_backslash_then_quote():
    assert tab_spawn._as_applescript_string(r'a"b\c') == r'a\"b\\c'


def test_hostile_argv_round_trips_through_both_layers():
    # An argv element full of shell + AppleScript metacharacters must survive
    # both quoting layers unchanged — proving neither can be escaped out of.
    argv = ["claude", "--resume", 'id"; rm -rf /; echo `x` $HOME \\ end']
    cmd = tab_spawn._shell_command(argv, cwd='/tmp/a b"c')
    # Shell layer: the command splits back to exactly cd + argv.
    assert shlex.split(cmd) == ["cd", '/tmp/a b"c', "&&", *argv]
    # AppleScript layer: unescaping recovers the shell command verbatim.
    literal = tab_spawn._as_applescript_string(cmd)
    assert _applescript_unescape(literal) == cmd


def test_terminal_applescript_runs_the_command_via_do_script():
    script = tab_spawn.terminal_applescript(["tmux", "attach", "-t", "crr-abc12345"], cwd=None)
    assert script.startswith('tell application "Terminal"')
    assert "do script" in script
    assert "tmux attach -t crr-abc12345" in script


def test_iterm_applescript_writes_text_to_a_new_session():
    script = tab_spawn.iterm_applescript(["tmux", "attach", "-t", "crr-abc12345"], cwd=None)
    assert 'tell application "iTerm"' in script
    assert "write text" in script
    assert "tmux attach -t crr-abc12345" in script


# --- terminal choice (pure; config prior + $TERM_PROGRAM) -----------------

@pytest.mark.parametrize("cfg,env,expected", [
    ("auto", {"TERM_PROGRAM": "iTerm.app"}, "iterm"),
    ("auto", {"TERM_PROGRAM": "Apple_Terminal"}, "terminal"),
    ("auto", {}, "terminal"),                              # default: always-present Terminal
    ("iterm", {"TERM_PROGRAM": "Apple_Terminal"}, "iterm"),   # config overrides env
    ("terminal", {"TERM_PROGRAM": "iTerm.app"}, "terminal"),
])
def test_choose_prefers_config_then_term_program(cfg, env, expected):
    assert tab_spawn.choose(cfg, env) == expected


# --- spawner wiring (captured, never executed) ----------------------------

def test_terminal_spawner_open_tab_invokes_osascript_with_the_built_script(monkeypatch):
    calls = []
    monkeypatch.setattr(tab_spawn.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    argv = ["tmux", "attach", "-t", "crr-abc12345"]
    tab_spawn.TerminalAppSpawner(5).open_tab(argv)
    assert calls == [["osascript", "-e", tab_spawn.terminal_applescript(argv, None)]]


def test_iterm_spawner_open_tab_invokes_osascript_with_the_built_script(monkeypatch):
    calls = []
    monkeypatch.setattr(tab_spawn.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    argv = ["tmux", "attach", "-t", "crr-abc12345"]
    tab_spawn.ITerm2Spawner(5).open_tab(argv)
    assert calls == [["osascript", "-e", tab_spawn.iterm_applescript(argv, None)]]


def test_available_reflects_open_dash_ra_returncode(monkeypatch):
    # `open -Ra <App>` returns 0 iff the app is registered — without launching
    # it. available() is a thin wrapper over that returncode.
    class _R:
        def __init__(self, rc): self.returncode = rc
    monkeypatch.setattr(tab_spawn.subprocess, "run", lambda cmd, **kw: _R(0))
    assert tab_spawn.TerminalAppSpawner(5).available() is True
    monkeypatch.setattr(tab_spawn.subprocess, "run", lambda cmd, **kw: _R(1))
    assert tab_spawn.ITerm2Spawner(5).available() is False


def test_spawner_for_maps_kind_to_class():
    assert isinstance(tab_spawn.spawner_for("terminal", 5), tab_spawn.TerminalAppSpawner)
    assert isinstance(tab_spawn.spawner_for("iterm", 5), tab_spawn.ITerm2Spawner)


# --- gated real-tool syntax check (macOS only) ----------------------------

def _app_registered(app: str) -> bool:
    try:
        return subprocess.run(["open", "-Ra", app], capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(shutil.which("osacompile") is None, reason="osacompile not available (non-macOS)")
def test_terminal_applescript_compiles(tmp_path):
    # The macOS syntax gate: osacompile compiles (resolving Terminal's
    # scripting dictionary) but does not send it the do-script event. Only
    # Terminal.app is guaranteed on the runner; iTerm is not, so its compile
    # check skips when unregistered.
    if not _app_registered("Terminal"):
        pytest.skip("Terminal.app not registered")
    script = tab_spawn.terminal_applescript(["tmux", "attach", "-t", "crr-abc12345"], cwd=None)
    out = tmp_path / "t.scpt"
    result = subprocess.run(
        ["osacompile", "-o", str(out), "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("osacompile") is None, reason="osacompile not available (non-macOS)")
def test_iterm_applescript_compiles(tmp_path):
    if not _app_registered("iTerm"):
        pytest.skip("iTerm2 not installed")
    script = tab_spawn.iterm_applescript(["tmux", "attach", "-t", "crr-abc12345"], cwd=None)
    out = tmp_path / "i.scpt"
    result = subprocess.run(
        ["osacompile", "-o", str(out), "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_osascript_spawner_reports_a_timeout_as_unconfirmed_not_failed(monkeypatch):
    # [#53/#54] tab_spawn_timeout_seconds feeds every spawner, not just WSL.
    # A slow-but-successful launch must not be asserted as a failure on any
    # platform — ports.TabSpawnTimeout is the contract, so every adapter owes
    # it.
    import subprocess as sp
    from crr.core.ports import TabSpawnTimeout

    def slow(cmd, **kw):
        raise sp.TimeoutExpired(cmd, kw.get("timeout", 30))

    monkeypatch.setattr(tab_spawn.subprocess, "run", slow)
    try:
        tab_spawn.TerminalAppSpawner(30).open_tab(["tmux", "attach", "-t", "crr-x"])
    except TabSpawnTimeout as exc:
        assert exc.seconds == 30
    else:
        raise AssertionError("expected TabSpawnTimeout")
