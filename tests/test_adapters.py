"""Adapter tests — the pure judgment logic, isolated from the OS.

The subprocess/OS edges (running `ps`, os.kill) are thin; the decisions
worth testing are the path resolution and the "does this tty string mean
a real terminal" rule, both extracted as pure helpers so they need no
platform gating.
"""

import inspect
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from crr.adapters import diagnostics, diagnostics_macos, diagnostics_windows
from crr.adapters import process_probe, state_dir, tmux
from crr.adapters import process_probe as pp  # short alias used below


# --- state_dir resolution (pure) -----------------------------------------

def test_state_dir_macos():
    home = Path("/Users/someone")
    got = state_dir.resolve("Darwin", env={}, home=home)
    assert got == home / "Library" / "Application Support" / "crr"


def test_state_dir_linux_respects_xdg():
    got = state_dir.resolve("Linux", env={"XDG_STATE_HOME": "/x/state"}, home=Path("/home/u"))
    assert got == Path("/x/state") / "crr"


def test_state_dir_linux_default_when_no_xdg():
    got = state_dir.resolve("Linux", env={}, home=Path("/home/u"))
    assert got == Path("/home/u") / ".local" / "state" / "crr"


# --- tty judgment (pure) --------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("pts/3", True),
    ("  pts/1  \n", True),
    ("ttys002", True),
    ("?", False),
    ("??", False),
    ("", False),
    ("   \n", False),
])
def test_tty_is_real(raw, expected):
    assert process_probe._tty_is_real(raw) is expected


def test_parse_tty_pids_keeps_only_real_ttys():
    # `ps -o tty=,pid=` output: tty first, pid last. Real ttys → their pids;
    # "?"/absent tty → dropped.
    out = process_probe._parse_tty_pids(
        "ttys001  100\n?        200\npts/3    300\n"
    )
    assert out == {100, 300}


def test_parse_tty_pids_tolerates_blank_and_short_lines():
    assert process_probe._parse_tty_pids("\n  \n999\n") == set()


def test_controlling_ttys_empty_pids_never_shells_out(monkeypatch):
    # A bare `ps` with no -p lists every process — controlling_ttys([]) must
    # short-circuit to an empty set instead.
    def _boom(*a, **k):
        raise AssertionError("ps must not run for an empty pid list")
    monkeypatch.setattr(process_probe.subprocess, "run", _boom)
    assert process_probe.PsProcessProbe(timeout_seconds=5).controlling_ttys([]) == set()


def test_controlling_ttys_batches_current_process(monkeypatch):
    # One ps call for all pids; the current process (which has a tty under a
    # terminal, or not under CI) is parsed correctly either way. Assert the
    # single batched invocation shape rather than tty presence (CI has none).
    calls = []
    real_run = process_probe.subprocess.run

    def _spy(cmd, **kw):
        calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(process_probe.subprocess, "run", _spy)
    probe = process_probe.PsProcessProbe(timeout_seconds=5)
    result = probe.controlling_ttys([os.getpid(), 1])
    assert len(calls) == 1  # batched: a single ps for both pids
    assert calls[0][:3] == ["ps", "-o", "tty=,pid="]
    assert isinstance(result, set)


# --- is_alive (real pids, POSIX) -----------------------------------------

def test_is_alive_true_for_current_process():
    probe = process_probe.PsProcessProbe(timeout_seconds=5)
    assert probe.is_alive(os.getpid()) is True


def test_is_alive_false_for_reaped_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child.terminate()
    child.wait()
    # Give the OS a beat to fully clear it.
    time.sleep(0.05)
    probe = process_probe.PsProcessProbe(timeout_seconds=5)
    assert probe.is_alive(child.pid) is False


# --- tmux command builders (pure) ----------------------------------------

def test_new_session_cmd_is_word_form_after_dashdash():
    cmd = tmux._new_session_cmd("crr-abc", "/home/u/p", ["claude", "--resume", "sid-1"])
    assert cmd == [
        "tmux", "new-session", "-d", "-s", "crr-abc", "-c", "/home/u/p",
        "--", "claude", "--resume", "sid-1",
    ]


def test_parse_sessions_drops_blank_lines():
    assert tmux._parse_sessions("a\nb\n\n") == {"a", "b"}
    assert tmux._parse_sessions("") == set()


def test_kill_session_cmd_targets_the_named_session():
    assert tmux._kill_session_cmd("crr-abc") == ["tmux", "kill-session", "-t", "crr-abc"]


# --- real tmux integration (gated on tmux installed) ---------------------

@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_realtmux_creates_word_form_detached_session(tmp_path, monkeypatch):
    # Isolate from the user's tmux by pointing the server at a scratch dir.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    t = tmux.RealTmux(timeout_seconds=10)
    try:
        assert t.list_sessions() == set()  # fresh server: no sessions
        t.new_detached_session("crr-itest", str(tmp_path), ["sleep", "300"])
        assert "crr-itest" in t.list_sessions()

        # Word-form proof: the pane runs the target directly, not a shell.
        got = subprocess.run(
            ["tmux", "display", "-t", "crr-itest", "-p", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=10,
        )
        assert got.stdout.strip() == "sleep"
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


# --- process controller pure builders (pure) ------------------------------

def test_parse_ps_rows_reads_pid_ppid_pgid_argv0():
    out = " 100 1 100 -fish\n 200 100 200 claude\n 201 200 200 node\n"
    assert pp._parse_ps_rows(out) == [
        (100, 1, 100, "-fish"),
        (200, 100, 200, "claude"),
        (201, 200, 200, "node"),
    ]


def test_parse_ps_rows_skips_malformed_lines():
    out = "100 1 100 claude\ngarbage\n\n200 100 200 node\n"
    assert pp._parse_ps_rows(out) == [(100, 1, 100, "claude"), (200, 100, 200, "node")]


def test_parse_ps_rows_with_args_column():
    out = "  100   1  100 -fish\n  200 100  200 claude --resume abc\n  bad line\n"
    assert pp._parse_ps_rows(out) == [
        (100, 1, 100, "-fish"),
        (200, 100, 200, "claude"),
    ]


def test_ps_snapshot_argv_includes_args():
    assert pp._ps_snapshot_argv() == ["ps", "-A", "-o", "pid=,ppid=,pgid=,args="]


def test_child_groups_returns_the_claude_group_not_the_shell_group():
    # shell pid 100 in its own group 100; its child 200 leads group 200
    # (claude under job control); 201 is claude's own child, same group 200.
    rows = [
        (100, 1, 100, "-fish"),
        (200, 100, 200, "claude"),
        (201, 200, 200, "node"),
        (999, 1, 999, "claude"),
    ]
    assert pp._child_groups(rows, shell_pid=100) == [200]


def test_child_groups_excludes_a_child_that_shares_the_shell_group():
    # Safety: a child in the SHELL's own group is never returned — signalling
    # it would kill the shell. Job-control-off is treated as "nothing to kick".
    rows = [(100, 1, 100, "-fish"), (200, 100, 100, "claude")]
    assert pp._child_groups(rows, shell_pid=100) == []


def test_child_groups_empty_when_shell_absent_or_childless():
    assert pp._child_groups([(100, 1, 100, "-fish")], shell_pid=100) == []
    assert pp._child_groups([(200, 100, 200, "claude")], shell_pid=100) == []


def test_child_groups_excludes_nonpositive_pgid():
    # pgid 0 would make os.killpg target the caller's own group — never return it.
    rows = [(100, 1, 100, "-fish"), (200, 100, 0, "claude")]
    assert pp._child_groups(rows, shell_pid=100) == []


def test_child_groups_selects_only_claude_children():
    """[bug 2026-07-29] kick killed every child group — a `make &` bg job died
    with the claude it was never part of. Selection is ancestry + argv0 basename
    prefix 'claude', never a global pattern."""
    rows = [
        (100, 1, 100, "-fish"),                       # the shell itself
        (200, 100, 200, "claude"),                    # claude child -> selected
        (300, 100, 300, "make"),                      # bg build -> NOT selected
        (400, 100, 400, "/usr/local/bin/claude"),     # abs path claude -> selected
        (500, 100, 500, "claude-fake"),               # test fake -> selected (prefix)
        (600, 200, 200, "node"),                      # grandchild, same group
    ]
    assert pp._child_groups(rows, 100) == [200, 400, 500]


# --- DiagnosticsSource port conformance -----------------------------------

def test_all_diagnostics_sources_satisfy_the_port():
    """DESIGN: diagnostics is an adapter interface. The de-facto contract
    (SOURCE_NAME / available / collect) is now a declared core port."""
    from crr.core.ports import DiagnosticsSource
    for module in (diagnostics, diagnostics_macos, diagnostics_windows):
        assert isinstance(module.SOURCE_NAME, str) and module.SOURCE_NAME
        assert callable(module.available)
        assert callable(module.collect)
        sig = inspect.signature(module.collect)
        assert len(sig.parameters) == 1   # collect(config)
