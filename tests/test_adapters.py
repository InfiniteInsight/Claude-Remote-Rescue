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
from crr.adapters import process_probe, state_dir, tailscale, tmux
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


def test_is_alive_does_not_kill_what_it_probes():
    # Not hypothetical: this failed on its first Windows run, with the
    # probed child returning exit 0xC0000142 after nothing but an is_alive()
    # call. os.kill(pid, 0) is a pure existence check on POSIX, but Windows
    # routes non-CTRL_* signals through TerminateProcess, so the probe was
    # lethal — a status read that ends the session it is reporting on, in a
    # tool whose entire job is not losing sessions. Fixed by [#74]'s
    # OpenProcess/GetExitCodeProcess path; this is the test that caught it
    # and the one that keeps it caught.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        probe = process_probe.PsProcessProbe(timeout_seconds=5)
        probe.is_alive(child.pid)
        time.sleep(0.05)
        assert child.poll() is None, (
            f"is_alive() killed the process it probed (exit {child.poll()}) — "
            "a status probe must never be a mutation"
        )
    finally:
        child.kill()
        child.wait(timeout=5)


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


# --- list_sessions tri-state (F16, spine — null-result expressibility) ---
#
# "no sessions" and "could not tell" used to both collapse to set(), which
# let a transient tmux query failure accumulate a revive strike as if the
# session were confirmed dead. list_sessions() now distinguishes: a
# genuine empty (no tmux server at all) returns set(); anything else
# unreadable returns None so callers can refuse to act rather than guess.

class _Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_current_session_cmd_asks_tmux_for_the_session_name():
    assert tmux._current_session_cmd() == ["tmux", "display-message", "-p", "#S"]


def test_current_session_name_parses_the_name(monkeypatch):
    monkeypatch.setattr(tmux.subprocess, "run",
                        lambda *a, **k: _Result(0, stdout="work\n"))
    assert tmux.RealTmux(timeout_seconds=5).current_session_name() == "work"


def test_current_session_name_is_none_when_not_in_tmux(monkeypatch):
    # `display-message` outside tmux exits nonzero ("no server"/"no current...").
    monkeypatch.setattr(tmux.subprocess, "run",
                        lambda *a, **k: _Result(1, stderr="no server running\n"))
    assert tmux.RealTmux(timeout_seconds=5).current_session_name() is None


def test_current_session_name_is_none_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=5)
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    assert tmux.RealTmux(timeout_seconds=5).current_session_name() is None


def test_list_sessions_returns_none_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=5)
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    assert tmux.RealTmux(timeout_seconds=5).list_sessions() is None


def test_list_sessions_returns_none_on_oserror(monkeypatch):
    def boom(*a, **k):
        raise OSError("tmux binary vanished mid-call")
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    assert tmux.RealTmux(timeout_seconds=5).list_sessions() is None


def test_list_sessions_returns_empty_set_when_no_server_running(monkeypatch):
    # tmux's own "there is no server" wording after a server existed and
    # was killed — a genuine, confident empty.
    monkeypatch.setattr(
        tmux.subprocess, "run",
        lambda *a, **k: _Result(1, stderr="no server running on /tmp/tmux-1000/default\n"),
    )
    assert tmux.RealTmux(timeout_seconds=5).list_sessions() == set()


def test_list_sessions_returns_empty_set_when_socket_never_existed(monkeypatch):
    # [inspect-and-decide] measured on this repo's tmux 3.4: a server that
    # was NEVER started (the common case — e.g. a fresh TMUX_TMPDIR, as the
    # RealTmux integration test below exercises) produces this message
    # instead of "no server running". Both mean the same thing: there is no
    # server, hence genuinely no sessions — not a query failure.
    monkeypatch.setattr(
        tmux.subprocess, "run",
        lambda *a, **k: _Result(
            1, stderr="error connecting to /tmp/tmux-1000/default (No such file or directory)\n"
        ),
    )
    assert tmux.RealTmux(timeout_seconds=5).list_sessions() == set()


def test_list_sessions_returns_none_on_an_unrecognized_nonzero_exit(monkeypatch):
    # Anything else nonzero is an unknown state, never silently "empty" —
    # a permissions error, a corrupted socket, etc. must not read as
    # "confirmed no sessions" (that would let a transient failure
    # accumulate a revive strike against a session that may still be alive).
    monkeypatch.setattr(
        tmux.subprocess, "run",
        lambda *a, **k: _Result(1, stderr="permission denied\n"),
    )
    assert tmux.RealTmux(timeout_seconds=5).list_sessions() is None


# --- real tmux integration (gated on tmux installed) ---------------------

@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_tmux_session_path(tmp_path, monkeypatch):
    # (#165) crr relies on this: a new session runs with the PATH of the
    # process that created it, not of whoever started the tmux server. So the
    # watchdog unit's PATH decides which claude a revival runs.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    out = tmp_path / "path.txt"
    base = os.environ["PATH"]
    t = tmux.RealTmux(timeout_seconds=10)
    try:
        monkeypatch.setenv("PATH", f"{first}{os.pathsep}{base}")
        t.new_detached_session("crr-a", str(tmp_path), ["sleep", "30"])  # starts the server
        monkeypatch.setenv("PATH", f"{second}{os.pathsep}{base}")
        t.new_detached_session("crr-b", str(tmp_path),
                               ["sh", "-c", f'echo "$PATH" > {out}; sleep 30'])
        deadline = time.monotonic() + 5
        while not (out.exists() and out.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert out.read_text().strip().split(os.pathsep)[0] == str(second)
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_spawn_path_overrides_the_callers_path(tmp_path, monkeypatch):
    # (#189) Because a session takes its creator's PATH, a revival started
    # from a login shell ran whatever `claude` that shell's PATH found: during
    # fish conf.d startup, a Windows npm shim that exits at once. spawn_path
    # gives the session the installed service's PATH instead.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    caller, service = tmp_path / "caller", tmp_path / "service"
    caller.mkdir()
    service.mkdir()
    out = tmp_path / "path.txt"
    base = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{caller}{os.pathsep}{base}")
    try:
        tmux.RealTmux(timeout_seconds=10).new_detached_session(
            "crr-a", str(tmp_path), ["sleep", "30"])  # the caller starts the server
        tmux.RealTmux(timeout_seconds=10, spawn_path=f"{service}{os.pathsep}{base}") \
            .new_detached_session("crr-b", str(tmp_path),
                                  ["sh", "-c", f'echo "$PATH" > {out}; sleep 30'])
        deadline = time.monotonic() + 5
        while not (out.exists() and out.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert out.read_text().strip().split(os.pathsep)[0] == str(service)
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


def test_spawn_path_runs_the_callers_tmux_with_the_service_path(monkeypatch):
    # The PATH goes to the tmux client (see new_detached_session for why not
    # `-e`), but the client binary stays the one every other RealTmux call in
    # this process runs, found through the caller's PATH.
    calls = []
    monkeypatch.setattr(tmux.shutil, "which",
                        lambda name: "/caller/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(tmux.subprocess, "run",
                        lambda cmd, **k: calls.append((cmd, k)) or _Result(0))
    tmux.RealTmux(timeout_seconds=5, spawn_path="/svc/bin:/usr/bin") \
        .new_detached_session("crr-abc", "/p", ["claude"])
    cmd, kwargs = calls[0]
    assert cmd == tmux._new_session_cmd("crr-abc", "/p", ["claude"])
    assert kwargs["env"]["PATH"] == "/svc/bin:/usr/bin"
    assert kwargs["env"]["HOME"] == os.environ["HOME"]  # only PATH changes
    assert kwargs["executable"] == "/caller/bin/tmux"


def test_no_spawn_path_leaves_the_tmux_call_as_it_was(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux.subprocess, "run",
                        lambda cmd, **k: calls.append((cmd, k)) or _Result(0))
    tmux.RealTmux(timeout_seconds=5).new_detached_session("crr-abc", "/p", ["claude"])
    _cmd, kwargs = calls[0]
    assert "env" not in kwargs and "executable" not in kwargs


def test_forward_title_commands_are_session_scoped(monkeypatch):
    # A reopened tab otherwise reads "wsl.exe": Claude Code sets the pane
    # title to the conversation name, but tmux (set-titles off) never
    # passes it to the outer terminal. Scope to the one session (-t, no -g)
    # so the user's own tmux sessions keep their configuration.
    calls = []
    monkeypatch.setattr(
        tmux.subprocess, "run",
        lambda cmd, **k: calls.append(cmd) or _Result(0),
    )
    tmux.RealTmux(timeout_seconds=5).forward_title("crr-abc")
    assert calls == [
        ["tmux", "set-option", "-t", "crr-abc", "set-titles", "on"],
        ["tmux", "set-option", "-t", "crr-abc", "set-titles-string", "#{pane_title}"],
    ]
    assert all("-g" not in c for c in calls)


def test_forward_title_never_raises(monkeypatch):
    # Cosmetic: a tmux failure must not cost the revival or the tab.
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("tmux", 5)
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    tmux.RealTmux(timeout_seconds=5).forward_title("crr-abc")  # no exception


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_new_session_forwards_the_pane_title(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    t = tmux.RealTmux(timeout_seconds=10)
    try:
        t.new_detached_session("crr-title", str(tmp_path), ["sleep", "300"])

        def opt(name):
            return subprocess.run(
                ["tmux", "show-options", "-v", "-t", "crr-title", name],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()

        assert opt("set-titles") == "on"
        assert opt("set-titles-string") == "#{pane_title}"
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


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
        # Polled, because the pane reads as "tmux" until its child has exec'd.
        command = ""
        deadline = time.monotonic() + 5
        while command != "sleep" and time.monotonic() < deadline:
            command = subprocess.run(
                ["tmux", "display", "-t", "crr-itest", "-p", "#{pane_current_command}"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        assert command == "sleep"
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


# --- attached_sessions: which parked sessions have a client (#32) ---------
#
# The dashboard shows every tmux-parked session as "restored". A session
# the user has already reopened has a client attached, and tmux knows it
# (#{session_attached}). attached_sessions() reads exactly those, so the
# card can say "attached" instead of "restored". It mirrors list_sessions'
# tri-state: unknown stays None and never a fabricated "nothing attached".

def test_attached_sessions_cmd_filters_on_session_attached():
    # -f keeps only sessions whose #{session_attached} is truthy (a client
    # is attached); -F prints just the name.
    assert tmux._attached_sessions_cmd() == [
        "tmux", "list-sessions", "-f", "#{session_attached}", "-F", "#{session_name}",
    ]


def test_attached_sessions_parses_names(monkeypatch):
    monkeypatch.setattr(
        tmux.subprocess, "run",
        lambda *a, **k: _Result(0, stdout="crr-abc\ncrr-def\n"),
    )
    assert tmux.RealTmux(timeout_seconds=5).attached_sessions() == {"crr-abc", "crr-def"}


def test_attached_sessions_empty_when_none_attached(monkeypatch):
    # Live server, sessions exist, none attached -> exit 0, empty stdout.
    monkeypatch.setattr(
        tmux.subprocess, "run", lambda *a, **k: _Result(0, stdout=""),
    )
    assert tmux.RealTmux(timeout_seconds=5).attached_sessions() == set()


# --- capture_pane / send_keys — dashboard reauth (spec 2026-08-21) --------

def test_capture_pane_cmd_uses_dash_j_to_join_wrapped_lines():
    assert tmux._capture_pane_cmd("crr-reauth") == [
        "tmux", "capture-pane", "-t", "crr-reauth", "-p", "-J",
    ]


def test_capture_pane_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(
        tmux.subprocess, "run",
        lambda *a, **k: _Result(0, stdout="If the browser didn't open, visit: https://x\n"),
    )
    out = tmux.RealTmux(timeout_seconds=5).capture_pane("crr-reauth")
    assert out == "If the browser didn't open, visit: https://x\n"


def test_capture_pane_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(tmux.subprocess, "run", lambda *a, **k: _Result(1, stderr="can't find session"))
    assert tmux.RealTmux(timeout_seconds=5).capture_pane("crr-reauth") is None


def test_capture_pane_returns_none_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=5)
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    assert tmux.RealTmux(timeout_seconds=5).capture_pane("crr-reauth") is None


def test_capture_pane_returns_none_on_oserror(monkeypatch):
    def boom(*a, **k):
        raise OSError("tmux binary vanished mid-call")
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    assert tmux.RealTmux(timeout_seconds=5).capture_pane("crr-reauth") is None


def test_send_keys_cmd_sends_text_then_enter():
    assert tmux._send_keys_cmd("crr-reauth", "abc123") == [
        "tmux", "send-keys", "-t", "crr-reauth", "abc123", "Enter",
    ]


def test_send_keys_calls_subprocess_with_the_built_command(monkeypatch):
    seen = []
    monkeypatch.setattr(
        tmux.subprocess, "run",
        lambda args, **k: seen.append(args) or _Result(0),
    )
    tmux.RealTmux(timeout_seconds=5).send_keys("crr-reauth", "abc123")
    assert seen == [["tmux", "send-keys", "-t", "crr-reauth", "abc123", "Enter"]]


def test_send_keys_swallows_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=5)
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    tmux.RealTmux(timeout_seconds=5).send_keys("crr-reauth", "abc123")  # must not raise


def test_send_keys_swallows_oserror(monkeypatch):
    def boom(*a, **k):
        raise OSError("tmux binary vanished mid-call")
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    tmux.RealTmux(timeout_seconds=5).send_keys("crr-reauth", "abc123")  # must not raise


def test_attached_sessions_empty_when_no_server(monkeypatch):
    monkeypatch.setattr(
        tmux.subprocess, "run",
        lambda *a, **k: _Result(1, stderr="no server running on /tmp/tmux-1000/default\n"),
    )
    assert tmux.RealTmux(timeout_seconds=5).attached_sessions() == set()


def test_attached_sessions_none_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=5)
    monkeypatch.setattr(tmux.subprocess, "run", boom)
    assert tmux.RealTmux(timeout_seconds=5).attached_sessions() is None


def test_attached_sessions_none_on_unrecognized_nonzero(monkeypatch):
    monkeypatch.setattr(
        tmux.subprocess, "run", lambda *a, **k: _Result(1, stderr="permission denied\n"),
    )
    assert tmux.RealTmux(timeout_seconds=5).attached_sessions() is None


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


def test_child_groups_includes_own_group_for_a_parked_wrapper_when_opted_in():
    # The reviver's `sh -c 'claude …'` exit-hook wrapper [/exit revival 2026-08-24] runs no job
    # control, so its claude child sits in the wrapper's OWN group. Kick on a
    # tmux-parked entry opts in via include_shell_group and must get that
    # group back — otherwise the wrapped session can never be kicked.
    rows = [(100, 1, 100, "sh"), (200, 100, 100, "claude")]
    assert pp._child_groups(rows, shell_pid=100, include_shell_group=True) == [100]


def test_child_groups_opt_in_never_returns_a_group_without_a_claude_child():
    # include_shell_group is not a blanket "return my own group": the group
    # is returned only because a claude child actually lives in it.
    rows = [(100, 1, 100, "sh"), (200, 100, 100, "make")]
    assert pp._child_groups(rows, shell_pid=100, include_shell_group=True) == []


def test_child_groups_opt_in_still_prefers_distinct_claude_groups():
    # A job-controlled claude child (its own group) is returned as before;
    # opting in only ADDS the own-group case, it doesn't replace it.
    rows = [(100, 1, 100, "sh"), (200, 100, 200, "claude")]
    assert pp._child_groups(rows, shell_pid=100, include_shell_group=True) == [200]


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


# --- _child_claude_pids: the DETECTION question (#184) ----------------------
# "Is this pid a claude process under this journaled shell?" is a question
# about processes, not groups. Keying it on group ids made every claude that
# did not lead its own group invisible to autokick, the reachability badge,
# keep-awake and the duplicate-conversation guard (measured 2026-11-06:
# keep-awake counted 2 of 7 running claudes). Signalling stays group-based in
# `_child_groups`; only detection widens.

def test_child_claude_pids_returns_a_job_controlled_claude():
    rows = [(100, 1, 100, "-fish"), (200, 100, 200, "claude"), (201, 200, 200, "node")]
    assert pp._child_claude_pids(rows, shell_pid=100) == [200]


def test_child_claude_pids_returns_a_claude_sharing_the_shell_group():
    # The case `_child_groups` must refuse to SIGNAL (it would kill the shell)
    # is still a running claude, and detection must see it.
    rows = [(100, 1, 100, "sh"), (200, 100, 100, "claude")]
    assert pp._child_claude_pids(rows, shell_pid=100) == [200]


def test_child_claude_pids_returns_the_journaled_pid_when_it_is_claude():
    # A revived session journaled under the claude process itself (#58).
    rows = [(200, 50, 200, "claude"), (201, 200, 200, "node")]
    assert pp._child_claude_pids(rows, shell_pid=200) == [200]


def test_child_claude_pids_selects_only_direct_claude_children():
    rows = [
        (100, 1, 100, "-fish"),
        (200, 100, 200, "claude"),
        (300, 100, 300, "make"),                      # not claude
        (400, 100, 100, "/usr/local/bin/claude"),     # shares the shell group
        (600, 200, 200, "claude-helper"),             # grandchild: not ours
        (700, 1, 700, "claude"),                      # someone else's claude
    ]
    assert pp._child_claude_pids(rows, 100) == [200, 400]


def test_child_claude_pids_empty_when_shell_absent_or_childless():
    assert pp._child_claude_pids([(100, 1, 100, "-fish")], shell_pid=100) == []
    assert pp._child_claude_pids([(200, 100, 200, "claude")], shell_pid=100) == []


def test_probe_claude_pids_batches_one_snapshot(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=(
            "100 1 100 -fish\n200 100 200 claude\n"
            "300 1 300 sh\n301 300 300 claude --resume x\n"), stderr="")

    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    out = pp.PsProcessProbe(2.0).claude_pids([100, 300, 999])
    assert out == {100: [200], 300: [301], 999: []}
    assert len(calls) == 1


def test_probe_claude_pids_degrades_to_empty(monkeypatch):
    monkeypatch.setattr(pp.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, stdout="", stderr="boom"))
    assert pp.PsProcessProbe(2.0).claude_pids([100]) == {}
    assert pp.PsProcessProbe(2.0).claude_pids([]) == {}


# --- find_resume_process (`crr adopt --takeover` live-process resolver) ---
#
# A shim/watchdog-launched claude carries `claude --resume <sid>` on argv
# (empirically verified via `ps`, see the adopt-takeover spec). This is a
# DIFFERENT specificity class from `_child_groups`'s ancestry+argv0-prefix
# selector (the kill-by-ancestry lesson) — one UUID matches one conversation,
# not "any process that looks like claude".

_SID = "93122659-c38d-4e24-a872-5521023a8cec"
_OTHER_SID = "aaaaaaaa-1111-4111-8111-111111111111"


def test_find_resume_process_matches_the_resume_sid(monkeypatch):
    stdout = f"796194    916 796194 claude --resume {_SID}\n"
    monkeypatch.setattr(
        pp.subprocess, "run",
        lambda *a, **k: _Result(0, stdout=stdout),
    )
    got = pp.PsProcessController(2.0).find_resume_process(_SID)
    assert got == pp.ResumeProcess(pid=796194, ppid=916, pgid=796194)


def test_find_resume_process_ignores_a_different_sid(monkeypatch):
    stdout = f"796194    916 796194 claude --resume {_OTHER_SID}\n"
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    assert pp.PsProcessController(2.0).find_resume_process(_SID) is None


def test_find_resume_process_ignores_claude_without_resume(monkeypatch):
    stdout = "796194    916 796194 claude\n"
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    assert pp.PsProcessController(2.0).find_resume_process(_SID) is None


def test_find_resume_process_empty_stdout_is_none(monkeypatch):
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=""))
    assert pp.PsProcessController(2.0).find_resume_process(_SID) is None


def test_find_resume_process_nonzero_returncode_is_none(monkeypatch):
    stdout = f"796194    916 796194 claude --resume {_SID}\n"
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(1, stdout=stdout))
    assert pp.PsProcessController(2.0).find_resume_process(_SID) is None


def test_find_resume_process_oserror_is_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("ps vanished")
    monkeypatch.setattr(pp.subprocess, "run", boom)
    assert pp.PsProcessController(2.0).find_resume_process(_SID) is None


def test_find_resume_process_whole_token_guard_no_prefix_false_hit(monkeypatch):
    # A row whose sid is a PREFIX of the queried sid (or vice-versa) must NOT
    # match — the check is a whole-argv-token match, not a loose substring.
    prefix_sid = _SID.split("-")[0]  # "93122659"
    stdout = f"796194    916 796194 claude --resume {prefix_sid}\n"
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    assert pp.PsProcessController(2.0).find_resume_process(_SID) is None
    # And the reverse: queried sid is the shorter prefix, row carries the full uuid.
    stdout2 = f"796194    916 796194 claude --resume {_SID}\n"
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout2))
    assert pp.PsProcessController(2.0).find_resume_process(prefix_sid) is None


def test_find_resume_process_returns_first_match_pid_ppid_pgid(monkeypatch):
    # Numbers must round-trip exactly, not just truthiness.
    stdout = (
        "100 1 100 -fish\n"
        f"500 100 500 claude --resume {_SID}\n"
    )
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    got = pp.PsProcessController(2.0).find_resume_process(_SID)
    assert (got.pid, got.ppid, got.pgid) == (500, 100, 500)


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


# --- resume_session_ids (batch liveness for the discoverable modal) -------

def test_resume_session_ids_collects_every_live_resume_sid(monkeypatch):
    stdout = (
        f"796194    916 796194 claude --resume {_SID}\n"
        f"797238 459365 797238 claude --resume {_OTHER_SID}\n"
        "659246 169702 659246 claude\n"                 # no --resume: ignored
        "123456      1 123456 sleep 3000\n"             # not claude: ignored
    )
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    assert pp.PsProcessController(2.0).resume_session_ids() == {_SID, _OTHER_SID}


# (#153) The sweep relies on this set, so it has to see the conversation however
# claude was started. `--session-id` is what the shims run for a fresh launch.
_LAUNCH_SHAPES = [
    f"--resume {_SID}",
    f"-r {_SID}",
    f"--resume={_SID}",
    f"--session-id {_SID}",
    f"--session-id={_SID}",
    f"--remote-control proj --session-id {_SID} --dangerously-skip-permissions",
]


@pytest.mark.parametrize("args", _LAUNCH_SHAPES)
def test_resume_session_ids_sees_every_way_claude_names_a_conversation(monkeypatch, args):
    stdout = f"796194    916 796194 claude {args}\n"
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    assert pp.PsProcessController(2.0).resume_session_ids() == {_SID}


@pytest.mark.parametrize("args", _LAUNCH_SHAPES)
def test_find_resume_process_agrees_on_every_launch_shape(monkeypatch, args):
    # A row the dashboard tags `running` has to be one Take over can find.
    stdout = f"796194    916 796194 claude {args}\n"
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    got = pp.PsProcessController(2.0).find_resume_process(_SID)
    assert got == pp.ResumeProcess(pid=796194, ppid=916, pgid=796194)


def test_a_bare_resume_picker_names_no_conversation(monkeypatch):
    stdout = "796194    916 796194 claude --resume --remote-control proj\n"
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    assert pp.PsProcessController(2.0).resume_session_ids() == set()


def test_a_wrapper_carrying_the_sid_is_not_the_running_claude(monkeypatch):
    # The reviver's exit hook, and the shell a tracked window starts in.
    stdout = (
        f'100 1 100 sh -c claude "$@"; crr deregister sh --resume {_SID}\n'
        f"200 1 200 fish -i -C claude --resume {_SID}\n"
    )
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(0, stdout=stdout))
    assert pp.PsProcessController(2.0).resume_session_ids() == set()


def test_resume_session_ids_is_one_ps_call_not_one_per_sid(monkeypatch):
    calls = []

    def fake_run(*a, **k):
        calls.append(1)
        return _Result(0, stdout=f"796194 916 796194 claude --resume {_SID}\n")

    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    pp.PsProcessController(2.0).resume_session_ids()
    assert calls == [1]  # a per-row probe would be O(rows) ps snapshots


def test_resume_session_ids_degrades_to_empty_on_probe_failure(monkeypatch):
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _Result(1, stdout=""))
    assert pp.PsProcessController(2.0).resume_session_ids() == set()

    def boom(*a, **k):
        raise OSError("no ps")

    monkeypatch.setattr(pp.subprocess, "run", boom)
    assert pp.PsProcessController(2.0).resume_session_ids() == set()


# --- session_pid: who is actually running in a parked session (#58) -------

def test_session_pid_cmd_asks_tmux_for_the_pane_pid():
    from crr.adapters import tmux as tmux_mod
    assert tmux_mod._session_pid_cmd("crr-abc") == [
        "tmux", "list-panes", "-t", "crr-abc", "-F", "#{pane_pid}",
    ]


def test_session_pid_parses_the_first_pane(monkeypatch):
    from crr.adapters import tmux as tmux_mod

    class R:
        returncode, stdout, stderr = 0, "2016\n", ""

    monkeypatch.setattr(tmux_mod.subprocess, "run", lambda *a, **k: R())
    assert tmux_mod.RealTmux(5).session_pid("crr-abc") == 2016


def test_session_pid_is_none_when_it_cannot_be_determined(monkeypatch):
    # Unknown is not zero (spine: null-result expressibility) — the reviver
    # must not re-key an entry onto a pid it did not actually observe.
    from crr.adapters import tmux as tmux_mod

    class Missing:
        returncode, stdout, stderr = 1, "", "can't find session"

    class Junk:
        returncode, stdout, stderr = 0, "not-a-number\n", ""

    for fake in (Missing, Junk):
        monkeypatch.setattr(tmux_mod.subprocess, "run", lambda *a, **k: fake())
        assert tmux_mod.RealTmux(5).session_pid("crr-abc") is None

    def boom(*a, **k):
        raise OSError("tmux gone")

    monkeypatch.setattr(tmux_mod.subprocess, "run", boom)
    assert tmux_mod.RealTmux(5).session_pid("crr-abc") is None


# --- tailscale adapter: status/serve_status tri-state JSON reader ---------
#
# Both commands wrap `tailscale ... --json`: missing binary, timeout,
# OSError, nonzero exit, and unparseable output all collapse to None,
# never a raise. Reuses this file's _Result fake rather than introducing a
# second fake-subprocess class.

def test_tailscale_status_cmd_is_json():
    assert tailscale._status_cmd() == ["tailscale", "status", "--json"]


def test_tailscale_serve_status_cmd_is_json():
    assert tailscale._serve_status_cmd() == ["tailscale", "serve", "status", "--json"]


def test_status_parses_json(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(
        tailscale.subprocess, "run",
        lambda *a, **k: _Result(0, stdout='{"Self": {"DNSName": "n.ts.net."}}'))
    assert ts.status() == {"Self": {"DNSName": "n.ts.net."}}


def test_status_none_when_binary_absent(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: None)
    assert ts.status() is None


def test_status_none_on_nonzero(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", lambda *a, **k: _Result(1, stdout=""))
    assert ts.status() is None


def test_status_none_on_garbage(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(
        tailscale.subprocess, "run", lambda *a, **k: _Result(0, stdout="not json"))
    assert ts.status() is None


def test_status_none_on_timeout(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")

    def _boom(*a, **k):
        raise tailscale.subprocess.TimeoutExpired(cmd="tailscale", timeout=2.0)

    monkeypatch.setattr(tailscale.subprocess, "run", _boom)
    assert ts.status() is None


def test_status_none_on_oserror(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")

    def _boom(*a, **k):
        raise OSError("tailscale binary vanished mid-call")

    monkeypatch.setattr(tailscale.subprocess, "run", _boom)
    assert ts.status() is None


def test_status_none_on_non_dict_json(monkeypatch):
    # `json.loads` succeeds but yields a list/scalar — still not a status dict.
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", lambda *a, **k: _Result(0, stdout="[1, 2]"))
    assert ts.status() is None


def test_available_reflects_which(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    assert ts.available() is True
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: None)
    assert ts.available() is False


def test_serve_status_parses_json(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(
        tailscale.subprocess, "run",
        lambda *a, **k: _Result(0, stdout='{"TCP": {"443": {"HTTPS": true}}}'))
    assert ts.serve_status() == {"TCP": {"443": {"HTTPS": True}}}


def test_serve_status_none_when_unconfigured_nonzero(monkeypatch):
    # `tailscale serve status --json` exits nonzero when serve has never been
    # configured — degrades to None, which the core layer treats as "serve
    # not live" rather than an error state.
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", lambda *a, **k: _Result(1, stdout=""))
    assert ts.serve_status() is None


def test_tailscale_provider_name_and_start_stop_argv(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()

    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", fake_run)
    ts = tailscale.RealTailscale(2.0)
    assert ts.name() == "tailscale"
    ok, _msg = ts.start(8377)
    assert ok
    assert calls[-1] == ["tailscale", "serve", "--bg", "8377"]
    ok, _msg = ts.stop()
    assert ok
    # Non-destructive: only the 443 handler is turned off — NEVER `serve
    # reset`, which clobbers unrelated serve config (spec 2026-09-02).
    assert calls[-1] == ["tailscale", "serve", "--https=443", "off"]


def test_tailscale_provider_health_tri_state(monkeypatch):
    ts = tailscale.RealTailscale(2.0)
    monkeypatch.setattr(ts, "serve_status", lambda: {"TCP": {"443": {}}})
    assert ts.health().state == "up"
    monkeypatch.setattr(ts, "serve_status", lambda: {})
    assert ts.health().state == "down"
    monkeypatch.setattr(ts, "serve_status", lambda: None)
    assert ts.health().state == "unknown"


def test_tailscale_provider_start_fails_without_binary(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: None)
    ts = tailscale.RealTailscale(2.0)
    ok, msg = ts.start(8377)
    assert not ok and "tailscale" in msg


# --- crr-launched claude must lead its own process group (autokick) --------
# Autokick asks "is the state file's claude pid one of claude_groups(journal
# pid)?" — a GROUP id. Both ways crr launches claude for you (the reviver's
# sh exit-hook wrapper and un-tmux's `fish -i -C`) left claude in its
# parent's group, so every such session read "unknown" and was never kicked.

def _wait_for_child(parent_pid, argv0_base, timeout=5.0):
    # Polled on argv0, not mere existence: between fork and exec the child is
    # still a copy of the shell (already in its new group under job control),
    # and a ps snapshot taken then would not show it as claude yet.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(["ps", "-o", "pid=,args=", "--ppid", str(parent_pid)],
                             capture_output=True, text=True).stdout.split(None, 2)
        if len(out) >= 2 and out[1].rsplit("/", 1)[-1] == argv0_base:
            return int(out[0])
        time.sleep(0.05)
    raise AssertionError(f"no {argv0_base} child of {parent_pid} appeared")


def _pgid(pid):
    return int(subprocess.run(["ps", "-o", "pgid=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip())


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_a_revived_claude_is_matched_by_autokick(tmp_path, monkeypatch):
    from crr.core.reviver import exit_hook_argv

    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    # A bare "claude" resolved through PATH, exactly as the reviver launches
    # it. (A long absolute argv0 would also be cut off by ps's 80-column
    # args when not on a tty, which is not what this test is about.) The
    # stand-in is the Python interpreter, not `sleep`: the multicall
    # coreutils on newer Ubuntu dispatch on argv0 and refuse to run as
    # "claude".
    (tmp_path / "claude").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    t = tmux.RealTmux(timeout_seconds=10)
    try:
        t.new_detached_session("crr-pg", str(tmp_path), exit_hook_argv(
            ["claude", "-c", "import time; time.sleep(300)"], "/bin/true"))
        deadline = time.monotonic() + 5
        pane = None
        while pane is None and time.monotonic() < deadline:
            pane = t.session_pid("crr-pg")
        child = _wait_for_child(pane, "claude")
        assert _pgid(child) == child, "claude must lead its own process group"
        controller = pp.PsProcessController(5)
        # autokick's pid_matched: the state-file pid is one of the groups
        assert child in controller.claude_groups(pane)
        # a parked kick signals claude's group only — never the pane's sh
        assert controller.claude_groups(pane, include_shell_group=True) == [child]
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.mark.skipif(shutil.which("tmux") is None or shutil.which("fish") is None,
                    reason="tmux and fish needed")
def test_fish_prefix_gives_the_command_its_own_process_group(tmp_path, monkeypatch):
    from crr.core.ops import FISH_OWN_JOB_GROUP

    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    t = tmux.RealTmux(timeout_seconds=10)
    try:
        # --no-config: never source the user's real crr shim from a test.
        t.new_detached_session("crr-fish", str(tmp_path), [
            "fish", "--no-config", "-i", "-C", FISH_OWN_JOB_GROUP + "sleep 300",
        ])
        deadline = time.monotonic() + 5
        pane = None
        while pane is None and time.monotonic() < deadline:
            pane = t.session_pid("crr-fish")
        child = _wait_for_child(pane, "sleep")
        assert _pgid(child) == child
        assert _pgid(child) != _pgid(pane)
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="POSIX process groups; the python-as-claude stand-in "
                           "is only verified to show as `claude` in ps on Linux")
def test_a_claude_sharing_its_launchers_group_is_detected_but_not_signalled(tmp_path):
    """#184, with real processes. The five sessions running when this was
    written had exactly this shape (launched before #178): claude in its
    launcher's process group. Detection must see it; signalling must still
    refuse the group, since for a live shell that group IS the shell."""
    (tmp_path / "claude").symlink_to(sys.executable)
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    # `; true` stops sh from exec'ing claude, so sh stays as the parent.
    # start_new_session makes sh its group's leader, as a pane process is.
    launcher = subprocess.Popen(
        ["sh", "-c", 'claude -c "import time; time.sleep(300)"; true'],
        env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        pids: list[int] = []
        while not pids and time.monotonic() < deadline:
            pids = pp.PsProcessProbe(5).claude_pids([launcher.pid]).get(launcher.pid, [])
            time.sleep(0.05)
        assert len(pids) == 1, "a claude sharing the launcher's group must be detected"
        assert _pgid(pids[0]) == launcher.pid, "precondition: it shares the group"
        assert pp.PsProcessController(5).claude_groups(launcher.pid) == []
    finally:
        os.killpg(launcher.pid, 9)
        launcher.wait()
