"""Linux desktop tab-spawn adapter tests (Phase 3).

The Linux terminals take an argv **directly** (no shell string), so there is
no escaping layer to get wrong — the command builders are pure and asserted
structurally, and the spawner wiring is tested by capturing the argv
(monkeypatched ``subprocess.run``) so no terminal is ever launched by the
suite. Runs on any platform (pure logic); only real spawning is Linux-GUI.
"""

from crr.adapters import tab_spawn_linux as tsl


_ARGV = ["tmux", "attach", "-t", "crr-abc12345"]


# --- pure command builders ------------------------------------------------

def test_gnome_terminal_runs_argv_after_double_dash():
    assert tsl.gnome_terminal_command(_ARGV, None) == ["gnome-terminal", "--", *_ARGV]


def test_gnome_terminal_threads_working_directory():
    cmd = tsl.gnome_terminal_command(_ARGV, "/home/u/p")
    assert cmd == ["gnome-terminal", "--working-directory", "/home/u/p", "--", *_ARGV]


def test_konsole_runs_argv_after_dash_e():
    assert tsl.konsole_command(_ARGV, None) == ["konsole", "-e", *_ARGV]
    assert tsl.konsole_command(_ARGV, "/w") == ["konsole", "--workdir", "/w", "-e", *_ARGV]


def test_kitty_runs_argv_directly():
    assert tsl.kitty_command(_ARGV, None) == ["kitty", *_ARGV]
    assert tsl.kitty_command(_ARGV, "/w") == ["kitty", "--directory", "/w", *_ARGV]


def test_wezterm_uses_cli_spawn():
    assert tsl.wezterm_command(_ARGV, None) == ["wezterm", "cli", "spawn", "--", *_ARGV]
    assert tsl.wezterm_command(_ARGV, "/w") == ["wezterm", "cli", "spawn", "--cwd", "/w", "--", *_ARGV]


# --- selection: config prior, then $TERM_PROGRAM/which --------------------

def test_choose_honors_an_explicit_linux_terminal_in_config():
    assert tsl.choose_kind("kitty", {}, which=lambda b: "/usr/bin/kitty") == "kitty"


def test_choose_ignores_a_macos_value_and_falls_back_to_detection():
    # "iterm"/"terminal" are macOS choices; on Linux they mean "auto".
    which = lambda b: "/usr/bin/konsole" if b == "konsole" else None
    assert tsl.choose_kind("iterm", {}, which=which) == "konsole"


def test_choose_auto_prefers_term_program_when_it_names_a_known_terminal():
    which = lambda b: "/usr/bin/" + b  # everything installed
    assert tsl.choose_kind("auto", {"TERM_PROGRAM": "WezTerm"}, which=which) == "wezterm"


def test_choose_auto_falls_back_to_first_installed_in_priority_order():
    which = lambda b: "/usr/bin/kitty" if b == "kitty" else None
    assert tsl.choose_kind("auto", {}, which=which) == "kitty"


def test_choose_returns_none_when_no_terminal_is_installed():
    assert tsl.choose_kind("auto", {}, which=lambda b: None) is None


# --- headless guard + detect() -------------------------------------------

def test_detect_is_none_without_a_display():
    # Headless (no $DISPLAY/$WAYLAND_DISPLAY) has no tabs — tmux-only. Even
    # with every terminal "installed", the display check short-circuits.
    only_kitty = lambda b: "/usr/bin/kitty" if b == "kitty" else None
    assert tsl.detect("auto", {}, timeout_seconds=5, which=only_kitty) is None


def test_detect_returns_a_spawner_with_a_display_and_an_installed_terminal():
    env = {"DISPLAY": ":0"}
    only_kitty = lambda b: "/usr/bin/kitty" if b == "kitty" else None
    got = tsl.detect("auto", env, timeout_seconds=5, which=only_kitty)
    assert isinstance(got, tsl.LinuxTerminalSpawner)
    assert got.kind == "kitty"


def test_detect_none_when_display_present_but_no_terminal():
    got = tsl.detect("auto", {"WAYLAND_DISPLAY": "wayland-0"}, timeout_seconds=5, which=lambda b: None)
    assert got is None


# --- spawner wiring (captured, never launched) ---------------------------

def test_spawner_open_tab_runs_the_built_command(monkeypatch):
    calls = []
    monkeypatch.setattr(tsl.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    tsl.LinuxTerminalSpawner("gnome-terminal", 5).open_tab(_ARGV)
    assert calls == [["gnome-terminal", "--", *_ARGV]]


def test_spawner_available_reflects_which(monkeypatch):
    monkeypatch.setattr(tsl.shutil, "which", lambda b: "/usr/bin/konsole")
    assert tsl.LinuxTerminalSpawner("konsole", 5).available() is True
    monkeypatch.setattr(tsl.shutil, "which", lambda b: None)
    assert tsl.LinuxTerminalSpawner("konsole", 5).available() is False
