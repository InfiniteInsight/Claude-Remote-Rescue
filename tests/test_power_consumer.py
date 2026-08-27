"""The power-block consumer: poll step, loop, and commands.

Phase 1a shipped adapters nothing called. These are the tests for the
wiring that finally calls them.
"""

import os
import queue
import signal
import subprocess
import sys
import threading
import time

import pytest

from crr import cli
from crr.adapters import power_state
from crr.core import power
from crr.core.config import DEFAULTS


# These tests deliver a real OS signal and check the receiving process's
# own handler runs in response. On Windows that channel does not exist:
# neither `os.kill(pid, SIGTERM)` nor `Popen.send_signal(SIGTERM)` deliver
# a signal at all -- CPython routes them through `TerminateProcess`, which
# kills the target outright instead of invoking its handler (and
# `Popen.send_signal(SIGINT)` isn't even reachable there -- it isn't
# `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`, so it hits Python's own
# `raise ValueError("Unsupported signal")` before anything is sent). This
# is the exact mechanism in #74 (`PsProcessProbe.is_alive`'s
# `os.kill(pid, 0)` was measured killing the process it probed), at a new
# call site: self-signalling the pytest process itself killed the CI run
# outright (no summary line, exit 1, dead mid-suite). The next person to
# see this skip should not "fix" it by swapping in CTRL_BREAK_EVENT or
# similar -- there is no real-signal equivalent to translate to.
_needs_real_signal_delivery = pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "delivers a real signal; on Windows os.kill/Popen.send_signal is "
        "TerminateProcess (not a signal), so it would kill the target "
        "outright instead of invoking its handler -- see #74"
    ),
)


def test_live_claude_count_counts_only_sessions_with_a_live_owner():
    # A journal entry with a claude field is not proof the agent is alive;
    # only the process snapshot is. Counting entries instead of owners
    # would hold the machine awake for sessions that already died.
    entries = [{"pid": 1}, {"pid": 2}, {"pid": 3}]
    owners = {1: [11], 2: [], 3: [33]}
    assert cli._live_claude_count(entries, owners) == 2


def test_live_claude_count_is_zero_when_nothing_is_owned():
    assert cli._live_claude_count([{"pid": 1}], {1: []}) == 0


def test_live_claude_count_treats_a_missing_owner_entry_as_not_live():
    # `claude_group_pids` omits pids it could not resolve. Absent is not
    # alive — the spine rule, applied to the thing that decides whether
    # crr keeps a laptop awake.
    assert cli._live_claude_count([{"pid": 9}], {}) == 0


def test_power_holder_threads_the_configured_cap():
    holder = cli._power_holder("Windows", wsl=False, max_hours=3)
    assert holder._max_hours == 3


def test_power_holder_cap_defaults_to_the_named_config_prior():
    holder = cli._power_holder("Windows", wsl=False)
    assert holder._max_hours == DEFAULTS["power_block_max_hours"]


class _FakeHolder:
    def __init__(self, caps=frozenset({"sleep", "shutdown"})):
        self._caps = caps
        self.calls = []
        self._held = frozenset()

    def capabilities(self):
        return self._caps

    def hold(self, want, reason):
        self.calls.append(("hold", want, reason))
        self._held = want & self._caps

    def release(self):
        self.calls.append(("release",))
        self._held = frozenset()

    def held(self):
        return self._held


class _FakeSource:
    def __init__(self, value):
        self.value = value

    def on_ac(self):
        return self.value


def _cfg(**over):
    base = {"power_block": "sleep+shutdown", "power_block_requires_ac": True}
    base.update(over)
    return base


def test_poll_holds_when_a_session_is_live_and_on_ac():
    holder, source = _FakeHolder(), _FakeSource(True)
    d = cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert d.want == frozenset({"sleep", "shutdown"})
    assert holder.calls[0][0] == "hold"
    assert "1 Claude session" in holder.calls[0][2]


def test_poll_releases_when_the_last_session_ends():
    holder, source = _FakeHolder(), _FakeSource(True)
    cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    holder.calls.clear()
    cli._power_poll_once(holder, source, [{"pid": 1}], {1: []}, _cfg())
    assert holder.calls == [("release",)]
    assert holder.held() == frozenset()


def test_poll_releases_on_battery():
    holder, source = _FakeHolder(), _FakeSource(False)
    d = cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert d.want == frozenset()
    assert d.withheld and "battery" in d.withheld
    assert holder.calls == [("release",)]


def test_poll_releases_when_the_power_source_cannot_be_read():
    holder, source = _FakeHolder(), _FakeSource(None)
    d = cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert d.want == frozenset()
    assert d.withheld and "cannot tell" in d.withheld
    assert holder.calls == [("release",)]


def test_poll_does_not_ask_the_source_when_ac_is_not_required():
    # A probe that is never consulted cannot fail, and on a desktop the
    # question is meaningless. Skipping it also keeps the poll cheap.
    class _Boom:
        def on_ac(self):
            raise AssertionError("power source consulted despite requires_ac=False")

    holder = _FakeHolder()
    d = cli._power_poll_once(holder, _Boom(), [{"pid": 1}], {1: [11]},
                             _cfg(power_block_requires_ac=False))
    assert d.want == frozenset({"sleep", "shutdown"})


def test_poll_is_idempotent_while_nothing_changes():
    holder, source = _FakeHolder(), _FakeSource(True)
    for _ in range(3):
        cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert [c[0] for c in holder.calls] == ["hold", "hold", "hold"]
    # The holder itself is responsible for making a repeat hold a no-op;
    # the poll step must not try to remember state the holder owns.


def test_poll_holds_only_what_the_platform_can_do():
    holder, source = _FakeHolder(caps=frozenset({"sleep"})), _FakeSource(True)
    cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert holder.held() == frozenset({"sleep"})


def test_awake_once_polls_exactly_once_and_exits_zero(tmp_path, monkeypatch, capsys):
    # --once must still release on the way out, even after a poll that
    # decided to hold. The hold is a CHILD PROCESS bounded by THIS
    # process's lifetime, not a durable OS-level reservation --once can
    # hand off and walk away from. On Linux/macOS that child
    # (`systemd-inhibit ... sleep infinity` / `caffeinate -i`) is spawned
    # with stdin=DEVNULL, so it has no liveness channel back to a dead
    # parent: skipping the release here would orphan it permanently, with
    # no handle and no visible cause, on every single cron tick. (An
    # earlier version of this test asserted ["hold"] only, on the theory
    # that --once should hand a successful hold off to the next tick --
    # that was measured to leak exactly the orphan described above and
    # was wrong; release-every-time is correct.)
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 30, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    assert cli.main(["awake", "--once"]) == 0
    assert [c[0] for c in holder.calls] == ["hold", "release"]


def test_awake_releases_when_the_loop_is_asked_to_stop(tmp_path, monkeypatch):
    # systemctl stop sends SIGTERM. The hold must not depend on the
    # holder's own stdin-EOF fallback for the ordinary stop path.
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 0, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))

    ticks = {"n": 0}

    def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise KeyboardInterrupt   # stands in for the stop signal

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    assert cli.main(["awake"]) == 0
    assert holder.calls[-1] == ("release",), holder.calls


def test_awake_releases_even_when_a_poll_raises(tmp_path, monkeypatch, capsys):
    # A transient probe failure must not leave the machine pinned awake
    # with no loop left to release it.
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 0, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})

    def boom(*a, **k):
        raise RuntimeError("journal unreadable")

    monkeypatch.setattr(cli, "_power_entries_and_owners", boom)
    rc = cli.main(["awake", "--once"])
    assert rc != 0
    assert holder.calls[-1] == ("release",)
    assert "journal unreadable" in capsys.readouterr().err


def test_awake_rereads_config_each_poll_so_turning_it_off_takes_effect(tmp_path, monkeypatch):
    # Without this you must restart the unit to change the setting, which
    # makes the off switch feel broken.
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    modes = iter(["sleep", "off"])
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": next(modes), "power_block_requires_ac": True,
                                 "power_poll_seconds": 0, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    ticks = {"n": 0}

    def fake_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    cli.main(["awake"])
    kinds = [c[0] for c in holder.calls]
    assert kinds[0] == "hold" and "release" in kinds[1:]


@_needs_real_signal_delivery
def test_awake_releases_on_a_real_sigterm_not_just_simulated_keyboardinterrupt(
    tmp_path, monkeypatch
):
    # The other stop-signal test stands SIGTERM in for KeyboardInterrupt
    # by raising it directly out of a patched time.sleep. That proves the
    # loop's own except/finally logic is correct but does NOT prove a
    # real SIGTERM ever reaches it -- Python installs a converting
    # handler for SIGINT but not for SIGTERM, so an unhandled SIGTERM
    # terminates the process immediately, skipping `finally` outright.
    # This test delivers an actual `os.kill(os.getpid(), SIGTERM)` mid-loop
    # and checks the hold still gets released, which only happens if
    # `_cmd_awake` installed its own SIGTERM handler.
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 0, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))

    previous_handler = signal.getsignal(signal.SIGTERM)
    ticks = {"n": 0}

    def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    try:
        assert cli.main(["awake"]) == 0
    finally:
        # Belt-and-braces: _cmd_awake is supposed to restore this itself,
        # but a failure partway through must not leave a raise-on-SIGTERM
        # handler installed for the rest of the test session.
        signal.signal(signal.SIGTERM, previous_handler)
    assert holder.calls[-1] == ("release",), holder.calls
    # _cmd_awake must not leak its SIGTERM handler into the rest of the
    # process once it returns.
    assert signal.getsignal(signal.SIGTERM) is previous_handler


def test_awake_once_does_not_orphan_the_holds_child_process(tmp_path, monkeypatch):
    # Not just the call list: a fake `hold()` that spawns a REAL child
    # process, mirroring how every platform holder actually works
    # (`systemd-inhibit ... sleep infinity`, `caffeinate -i`, the Windows
    # interop child), and a `release()` that tears it down. --once must
    # leave no such child behind. On Linux/macOS the child's stdin is
    # already closed (DEVNULL), so it has no liveness channel back to a
    # dead parent -- an un-released hold here is a PERMANENT orphan, not
    # merely a delayed cleanup, and this is what caught that: an earlier
    # version of this loop skipped the release for a successful --once
    # poll, and this test's process would have still been alive with no
    # handle left to stop it.
    class _RealChildHolder:
        def __init__(self):
            self.calls = []
            self.proc = None

        def capabilities(self):
            return frozenset({"sleep"})

        def hold(self, want, reason):
            self.calls.append(("hold", want, reason))
            if self.proc is None:
                self.proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL,
                )

        def release(self):
            self.calls.append(("release",))
            if self.proc is not None:
                self.proc.terminate()
                self.proc.wait(timeout=5)
                self.proc = None

        def held(self):
            return frozenset({"sleep"}) if self.proc is not None else frozenset()

    holder = _RealChildHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 30, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))

    try:
        assert cli.main(["awake", "--once"]) == 0
        assert holder.proc is None, "release() must have cleared its child handle"
    finally:
        # Belt-and-braces: if the assertion above ever fails, don't leave
        # a real process running past this test.
        if holder.proc is not None:
            holder.proc.kill()
            holder.proc.wait(timeout=5)


# --- harness for the two "second signal lands mid-release" tests below ---
#
# These send TWO real OS signals at a running `crr awake`. The first
# version of this test signaled the pytest process itself (via
# os.kill(os.getpid(), ...)). Under a mutation that removed the SIG_IGN
# guard, the second signal re-entered _stop() and raised an unhandled
# KeyboardInterrupt out of `holder.release()` -- which, because it was
# delivered to the pytest process's own main thread mid-test, escaped
# the test entirely and aborted the whole pytest SESSION (not just this
# test): later tests showed as "deselected", not failed. A regression
# that looks like broken CI infrastructure is worse than the bug it
# guards against. So these two tests run `crr awake` as a genuine CHILD
# process instead and signal THAT -- a mutation can only ever kill the
# child, which shows up as a normal non-zero/timeout assertion failure.
_AWAKE_HARNESS = '''\
import pathlib
import sys
import time

from crr import cli


class _FakeSource:
    def on_ac(self):
        return True


class _SlowReleaseHolder:
    def capabilities(self):
        return frozenset({{"sleep"}})

    def hold(self, want, reason):
        print("hold", flush=True)

    def release(self):
        print("release-start", flush=True)
        time.sleep({ladder})   # stand-in for a slow teardown ladder
        print("release-done", flush=True)

    def held(self):
        return frozenset()


holder = _SlowReleaseHolder()
cli._power_holder = lambda *a, **k: holder
cli._power_source = lambda *a, **k: _FakeSource()
cli.state_dir.state_dir = lambda: pathlib.Path({tmp_path!r})
cli._load_config = lambda: {{
    "power_block": "sleep",
    "power_block_requires_ac": True,
    "power_poll_seconds": {poll_seconds},
    "power_block_max_hours": 12,
    "interop_timeout_seconds": 5,
}}
cli._power_entries_and_owners = lambda *a, **k: ([{{"pid": 1}}], {{1: [11]}})

rc = cli.main(["awake"])
print("rc=" + str(rc), flush=True)
sys.exit(rc)
'''


def _line_reader_queue(proc):
    """Start (once) a background thread draining ``proc.stdout`` into a
    queue, and return that queue.

    ``readline()`` has no native timeout, and unlike POSIX, Windows
    ``select`` accepts only sockets -- never pipes or file objects, which
    is exactly what a subprocess's ``stdout`` is (``OSError: [WinError
    10038] An operation was attempted on something that is not a
    socket``). A thread + queue is the portable way to bound what would
    otherwise be an unbounded blocking read on either platform.

    The thread (and its queue) is cached on ``proc`` itself so repeated
    calls for the same process reuse it -- a second wait for a later
    marker on the same ``proc.stdout`` must read from the SAME queue,
    never spawn a second reader thread racing the first for the next
    line off the pipe (whichever thread wins a given ``readline()`` would
    silently steal a line the other call was waiting on).
    """
    q = getattr(proc, "_marker_queue", None)
    if q is not None:
        return q
    q = queue.Queue()
    proc._marker_queue = q

    def _drain():
        try:
            for line in iter(proc.stdout.readline, ""):
                q.put(line)
        finally:
            q.put(None)  # sentinel: EOF

    threading.Thread(target=_drain, daemon=True).start()
    return q


def _wait_for_marker(proc, marker, output_lines, timeout):
    """Read lines from ``proc.stdout`` until one contains ``marker``.

    Bounded by ``timeout`` via the shared reader queue (see
    ``_line_reader_queue``) so a hung/misbehaving child cannot block the
    test suite forever. Returns False on timeout or EOF (process exited
    without ever printing the marker) instead of raising, so callers get
    a normal assertion failure with the captured output.
    """
    q = _line_reader_queue(proc)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            line = q.get(timeout=remaining)
        except queue.Empty:
            return False
        if line is None:
            return False  # EOF: the child exited before printing it
        output_lines.append(line.rstrip("\n"))
        if marker in line:
            return True


def _drain_remaining_output(proc, output_lines, timeout):
    """Collect whatever else the background reader thread (already
    started by an earlier ``_wait_for_marker`` call for this ``proc``)
    puts on the queue, until EOF or ``timeout``.

    Must be used instead of a raw ``proc.stdout.read()`` once
    ``_wait_for_marker`` has run for this ``proc`` -- the reader thread
    already owns all further reads off the pipe, so a second, direct read
    from the main thread would race it instead of seeing the same bytes.
    """
    q = _line_reader_queue(proc)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            line = q.get(timeout=remaining)
        except queue.Empty:
            return
        if line is None:
            return
        output_lines.append(line.rstrip("\n"))


def _run_awake_and_signal_twice(tmp_path, sig, poll_seconds=30, ladder=1.0, gap=0.4):
    """Spawn `crr awake` as a real child process, send ``sig`` once while
    it idles between polls (the ordinary stop path), then again ``gap``
    seconds into its (simulated, ``ladder``-second) release -- while
    release is still running. Returns ``(returncode, output_lines)``.
    """
    script = tmp_path / "run_awake.py"
    script.write_text(_AWAKE_HARNESS.format(
        tmp_path=str(tmp_path), poll_seconds=poll_seconds, ladder=ladder,
    ))
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output_lines = []
    try:
        # Startup-bound guard, not a behavior timeout: the child must spawn a
        # fresh interpreter and import crr before its first poll, which can run
        # slow on a loaded CI box. The wait returns the instant the marker
        # appears, so a generous ceiling only affects the hang case.
        assert _wait_for_marker(proc, "hold", output_lines, timeout=30), (
            f"child never reached its first poll; output so far: {output_lines}")
        proc.send_signal(sig)   # first stop signal, while idling between polls
        assert _wait_for_marker(proc, "release-start", output_lines, timeout=15), (
            f"child never entered release(); output so far: {output_lines}")
        time.sleep(gap)   # let release() get partway through its ladder
        proc.send_signal(sig)   # second stop signal, mid-release
        _drain_remaining_output(proc, output_lines, timeout=5)
        rc = proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    return rc, output_lines


@_needs_real_signal_delivery
def test_awake_finishes_release_even_when_a_second_sigterm_lands_mid_release(tmp_path):
    # A second SIGTERM landing WHILE release() is running (`systemctl
    # restart` re-sending SIGTERM mid-teardown) must not abort the
    # release with the hold's handle already gone. Release ladders run
    # up to 15s (systemd-inhibit teardown, the Windows child's stop
    # sequence) -- long enough for a second signal to be realistic.
    # Fixed by ignoring SIGTERM for the duration of the release path
    # specifically, rather than leaving whatever handler preceded ours in
    # place -- that is usually SIG_DFL (instant kill), which is the exact
    # bug this guards against.
    rc, output = _run_awake_and_signal_twice(tmp_path, signal.SIGTERM)
    assert rc == 0, output
    assert "release-done" in output, output


@_needs_real_signal_delivery
def test_awake_finishes_release_even_when_a_second_sigint_lands_mid_release(tmp_path):
    # The double-Ctrl-C half of the same finding. SIGINT already raises
    # KeyboardInterrupt via Python's own default handler -- with no guard,
    # a second one landing mid-release raises it AGAIN, right back into
    # `holder.release()`, aborting it exactly like an unguarded second
    # SIGTERM does. Verified before the fix: two SIGINTs 0.5s apart into a
    # 1.5s release left "release-done" never printed.
    rc, output = _run_awake_and_signal_twice(tmp_path, signal.SIGINT)
    assert rc == 0, output
    assert "release-done" in output, output


# Fix round 1 (2026-08-13): `crr power`/`crr doctor` used to ask a
# freshly-constructed holder `.held()` for what is held. Measured on this
# host: with a REAL `crr awake` holding (a live holder-child pid), a
# separate `crr power` process printed "holding: nothing" -- `.held()`
# only ever answers about a child THAT process's holder spawned, never a
# separate process's. The fix is a state file the awake loop stamps after
# every poll (crr.core.power.snapshot/interpret,
# crr.adapters.power_state); these tests write that file directly rather
# than relying on a holder's in-memory `.held()`, because that in-memory
# state is exactly what a separate reader process can never see.

def _POWER_CFG(**over):
    base = {"power_block": "sleep", "power_block_requires_ac": True,
            "power_poll_seconds": 30, "power_block_max_hours": 12,
            "interop_timeout_seconds": 5, "power_state_max_age_multiplier": 3}
    base.update(over)
    return base


def test_power_reports_what_is_held_and_why(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 2 Claude sessions live", os.getpid(), time.time(), want=frozenset({"sleep"})))
    assert cli.main(["power"]) == 0
    out = capsys.readouterr().out
    assert "sleep" in out


def test_power_names_the_release_command_whenever_something_is_held(
        tmp_path, monkeypatch, capsys):
    # The block must never be a trap: if crr is holding the machine
    # awake, the way to stop it has to be on screen.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "r", os.getpid(), time.time(), want=frozenset({"sleep"})))
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "crr power --release" in out or "stop" in out


def test_power_reports_the_withheld_reason_when_nothing_is_held(
        tmp_path, monkeypatch, capsys):
    # "crr is holding nothing" is useless without the reason -- and that
    # reason has to come from what the loop actually recorded (the state
    # file), not from this separate process recomputing its own guess.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(False))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, power.snapshot(
        frozenset(), "on battery (power_block_requires_ac is true)",
        os.getpid(), time.time(), want=frozenset()))
    cli.main(["power"])
    assert "battery" in capsys.readouterr().out


def test_power_states_capabilities_this_platform_lacks(tmp_path, monkeypatch, capsys):
    # macOS cannot block a shutdown. Silently holding half of what was
    # asked, and reporting success, is the failure this project keeps
    # finding. `unmet` is computed live (unaffected by the state-file
    # fix -- `capabilities()`/`decide()` do no I/O and never depended on
    # `.held()`), so no state file is needed for this one.
    monkeypatch.setattr(cli, "_power_holder",
                        lambda *a, **k: _FakeHolder(caps=frozenset({"sleep"})))
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: _POWER_CFG(power_block="sleep+shutdown"))
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "shutdown" in out and "unavailable" in out.lower()


def test_power_release_stops_the_unit_rather_than_pretending(
        tmp_path, monkeypatch, capsys):
    # The hold is a child of `crr awake`; this process has no handle to
    # it. Stopping the unit IS the release.
    ran = []
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    assert cli.main(["power", "--release"]) == 0
    assert ran and "stop" in " ".join(ran[0])


def test_power_release_clears_the_state_file_when_the_stop_succeeds(
        tmp_path, monkeypatch, capsys):
    # A released hold must not leave a stale claim behind for the next
    # `crr power`/`crr doctor` to read.
    #
    # RETARGETED (final fix wave, 2026-08-13). This test used to write the
    # snapshot with `os.getpid()` -- a writer that is ALIVE and FRESH --
    # and assert it was cleared, which is exactly the false clear Important
    # 2 is about: it encoded the `ok` disjunct rather than the property.
    # On the real healthy path the loop the stop command killed is DEAD by
    # the time this reads the file, so a dead writer pid is what the
    # scenario actually looks like. The property under test is unchanged
    # and undiminished: a successful release leaves no stale claim behind.
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    stopped = subprocess.Popen([sys.executable, "-c", "pass"])
    stopped.wait()
    time.sleep(0.05)
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", stopped.pid,
        time.time(), want=frozenset({"sleep"})))
    assert cli.main(["power", "--release"]) == 0
    assert power_state.read(tmp_path) is None
    assert "did NOT clear" not in capsys.readouterr().err


def test_power_release_does_not_clear_a_fresh_live_claim_when_the_stop_fails(
        tmp_path, monkeypatch, capsys):
    # Fix round 3 (2026-08-13): round 2's "clear unconditionally" was
    # ITSELF a bug. Measured: a WEDGED loop (SIGSTOPped, its hold's child
    # still genuinely alive) fails to stop -- `systemctl --user stop`
    # exits nonzero because the process never actually goes away -- and
    # its stale-looking timestamp (in a real wedge, `unknown -- last
    # report is 43s old`) was the ONLY evidence something was wrong.
    # Clearing it unconditionally fabricated a fresh "holding: nothing --
    # no keep-awake loop has reported" while the hold was still active: a
    # false KNOWN-nothing standing in for a live claim, the exact class
    # this whole feature exists to end. So here the claim is FRESH and
    # TRUSTWORTHY (a live pid, a recent timestamp) and the stop FAILS --
    # the file must survive, and the user must be told why on stderr.
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: False)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", os.getpid(), time.time(), want=frozenset({"sleep"})))
    assert cli.main(["power", "--release"]) == 1
    assert power_state.read(tmp_path) is not None  # the live claim survives
    err = capsys.readouterr().err
    assert "did NOT clear" in err
    assert "live report" in err


def test_power_release_clears_an_already_untrustworthy_claim_even_when_the_stop_fails(
        tmp_path, monkeypatch, capsys):
    # The complement: there is nothing left to protect when the file's
    # OWN claim is already untrustworthy (here: a dead writer) -- the
    # crashed-loop recovery case fix round 2 was originally written for
    # still needs to work, just narrowed to not also catch a genuinely
    # live claim.
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: False)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    time.sleep(0.05)
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", dead.pid, time.time(), want=frozenset({"sleep"})))
    assert cli.main(["power", "--release"]) == 1  # the stop command's own failure is still reported
    assert power_state.read(tmp_path) is None      # but the stale file is gone regardless
    assert "did NOT clear" not in capsys.readouterr().err


def test_power_release_clears_a_stale_claim_even_when_the_stop_fails(
        tmp_path, monkeypatch, capsys):
    # Same complement, staleness instead of a dead writer.
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: False)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: _POWER_CFG(power_poll_seconds=10,
                                           power_state_max_age_multiplier=3))
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live",
        os.getpid(), time.time() - 1000, want=frozenset({"sleep"})))  # far past 10 * 3 = 30s
    assert cli.main(["power", "--release"]) == 1
    assert power_state.read(tmp_path) is None


def test_power_release_clears_an_unreadable_claim_even_when_the_stop_fails(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: False)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    power_state.path_for(tmp_path).write_text('{"held": ["sleep"], "pid":', encoding="utf-8")
    assert cli.main(["power", "--release"]) == 1
    assert power_state.read(tmp_path) is None


def test_power_release_clears_when_no_file_ever_existed_and_the_stop_fails(
        tmp_path, monkeypatch, capsys):
    # Nothing to protect (and clearing a missing file is a no-op) -- and
    # the decline message must not fire for a file that never existed.
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: False)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    assert cli.main(["power", "--release"]) == 1
    assert power_state.read(tmp_path) is None
    assert "did NOT clear" not in capsys.readouterr().err


def test_power_release_clearing_a_missing_file_does_not_raise(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    assert cli.main(["power", "--release"]) == 0


def test_power_names_both_the_live_and_the_state_file_reason_when_nothing_is_held(
        tmp_path, monkeypatch, capsys):
    # Regression pin (fix round 1, review pass 2): the original Task 6
    # STOP condition on a `power_block=off` host expected
    # "holding: nothing — power_block is off". Moving the "nothing held"
    # source to the state file briefly LOST that fact (it printed only
    # "no keep-awake loop has reported", never naming the config). Both
    # must appear: the live reason answers "why isn't crr holding
    # anything right now", the state-file reason answers "has the loop
    # ever run".
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG(power_block="off"))
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    # No power_state.write() here -- this is the real default state on a
    # fresh host: `crr-awake` has never run.
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "holding: nothing — power_block is off / no keep-awake loop has reported" in out


def test_power_does_not_double_print_when_live_and_state_file_reasons_agree(
        tmp_path, monkeypatch, capsys):
    # `crr awake` last wrote the SAME reason `decide()` computes live right
    # now (the ordinary steady-state case: an up-to-date loop and a fresh
    # `crr power` call agree). The two must collapse into one clause, not
    # print the same sentence twice joined by " / ".
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG(power_block="off"))
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    power_state.write(tmp_path, power.snapshot(
        frozenset(), "power_block is off", os.getpid(), time.time(), want=frozenset()))
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "holding: nothing — power_block is off" in out
    assert "power_block is off / power_block is off" not in out


def test_power_state_pid_zero_is_unknown_not_a_claim(tmp_path, monkeypatch, capsys):
    # A corrupt/truncated power.json carrying pid 0 alongside a
    # fresh-looking timestamp must not license a positive hold claim:
    # os.kill(0, 0) targets the CALLER's own process group and would read
    # as "alive" if not guarded.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", 0, time.time(), want=frozenset({"sleep"})))
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "unknown" in out.lower()
    assert "holding: sleep" not in out
    assert "holding: nothing" not in out


def test_power_prints_unknown_when_the_writer_is_dead(tmp_path, monkeypatch, capsys):
    # A crashed/killed `crr awake` leaves its last snapshot on disk.
    # Reading that as "holding: nothing" would be a false all-clear: once
    # the writer is gone, whatever the OS actually did with that last hold
    # is genuinely unknown, not "released".
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    time.sleep(0.05)  # give the OS a beat to fully clear the pid
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", dead.pid, time.time(), want=frozenset({"sleep"})))
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "unknown" in out.lower()
    assert "holding: nothing" not in out  # must not read as the all-clear


def test_power_prints_unknown_when_the_last_report_is_stale(tmp_path, monkeypatch, capsys):
    # A wedged loop (hung, blocked on I/O) stops polling without dying --
    # `is_alive` alone would still call it trustworthy. The timestamp is
    # what catches that.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: _POWER_CFG(power_poll_seconds=10,
                                           power_state_max_age_multiplier=3))
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live",
        os.getpid(), time.time() - 1000, want=frozenset({"sleep"})))  # far past 10 * 3 = 30s
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "unknown" in out.lower()
    assert "holding: nothing" not in out


def test_doctor_names_what_power_is_holding(tmp_path, monkeypatch, capsys):
    # `crr doctor` must never omit an active hold, and the line must be
    # internally consistent. The regression this pins: an earlier version
    # of this test patched neither `_load_config` nor
    # `_power_entries_and_owners`, so doctor fell through to real defaults
    # (mode "off") while a `_FakeHolder` still claimed "sleep" was held --
    # measured output was "holding sleep — " with nothing after the dash,
    # and this test's own assertions passed anyway. Every input is pinned
    # here so the line asserted is the one a real hold actually produces.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", os.getpid(), time.time(), want=frozenset({"sleep"})))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    # The exact line, not just a substring match on either half -- the
    # bug this pins was each half individually present while their
    # combination was malformed ("holding sleep — " with nothing after).
    assert ("  [ok  ] power hold — holding sleep — crr: 1 Claude session live; "
            "release with: crr power --release") in out


def test_doctor_names_unknown_when_the_writer_is_dead(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    time.sleep(0.05)
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", dead.pid, time.time(), want=frozenset({"sleep"})))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "power hold" in out
    assert "unknown" in out.lower()
    assert "holding nothing" not in out


def test_doctor_warns_when_a_real_mode_is_configured_but_no_loop_has_ever_reported(
        tmp_path, monkeypatch, capsys):
    # Fix round 2 (2026-08-13): "power_block=off, no loop has reported"
    # is the correct, harmless default and stays [ok]. But a user who
    # configured `power_block=sleep` and whose `crr-awake` has never once
    # reported is asking for protection and getting none -- a green check
    # here is the same "succeeds loudly, protects nothing" failure this
    # whole feature exists to end, just moved from the hold itself to the
    # health check ABOUT the hold.
    #
    # `crr doctor` parses config.toml DIRECTLY (its own comment explains
    # why: its parse attempt doubles as the source `config` for the
    # systemctl check, so a second independent `_load_config()` call
    # would double-print a warning) -- monkeypatching `cli._load_config`
    # has NO effect on doctor's power_block. A real config.toml is
    # required here.
    (tmp_path / "config.toml").write_text('power_block = "sleep"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    # No power_state.write() -- the loop has genuinely never reported.
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[WARN] power hold" in out
    assert "holding nothing" in out


def test_doctor_stays_ok_when_power_block_is_off_and_no_loop_has_reported(
        tmp_path, monkeypatch, capsys):
    # The default, harmless state on a fresh install: nothing has ever
    # asked for protection, so nothing missing it is not a warning.
    # power_block=off is the DEFAULT (no config.toml needed to assert it).
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[ok  ] power hold" in out


def test_doctor_stays_ok_when_a_real_mode_is_configured_and_the_loop_is_running_but_idle(
        tmp_path, monkeypatch, capsys):
    # Distinguish "never reported" from "reported, and legitimately has
    # nothing to hold right now" (e.g. no live session this poll) -- the
    # latter is the loop doing its job correctly and must stay [ok].
    (tmp_path / "config.toml").write_text('power_block = "sleep"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    power_state.write(tmp_path, power.snapshot(
        frozenset(), "no live claude session", os.getpid(), time.time(), want=frozenset()))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[ok  ] power hold" in out


# --- the reviewer's three exact corrupt-input probes, at the CLI level -----
# (fix round 2, 2026-08-13) -- measured with a hold GENUINELY ACTIVE
# (powershell.exe child confirmed alive via tasklist.exe): a truncated
# power.json read as the all-clear, and a malformed `held` field either
# crashed both commands or silently rendered letter-soup as a real answer.
# Neither `crr power` nor `crr doctor` may raise on any of these, and both
# must say "unknown", never "nothing" and never a garbage held set.

def test_power_survives_truncated_json_and_reports_unknown(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    power_state.path_for(tmp_path).write_text('{"held": ["sleep"], "pid":', encoding="utf-8")
    rc = cli.main(["power"])  # must not raise
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out.lower()
    assert "holding: nothing" not in out
    assert "holding: sleep" not in out


def test_doctor_survives_truncated_json_and_reports_unknown(tmp_path, monkeypatch, capsys):
    # power_block defaults to "off" here (no config.toml written): the
    # unknown branch is [WARN] unconditionally, regardless of mode -- see
    # the next test for the "real mode + corrupt file" combination the
    # reviewer specifically measured (hold genuinely active, file
    # truncated), verified rather than inferred from this one.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    power_state.path_for(tmp_path).write_text('{"held": ["sleep"], "pid":', encoding="utf-8")
    rc = cli.main(["doctor"])  # must not raise
    out = capsys.readouterr().out
    assert rc == 0
    assert "[WARN] power hold" in out
    assert "unknown" in out.lower()


def test_doctor_warns_on_a_corrupt_file_while_a_real_mode_is_configured(
        tmp_path, monkeypatch, capsys):
    # The reviewer's exact measured conditions: `power_block` names a
    # real mode (a hold was genuinely being asked for) AND the state file
    # is corrupt (a hold may genuinely be active but unreadable). Checked
    # directly rather than inferred from the "off" case above -- the
    # unknown branch and the never-reported branch are different code
    # paths in `_cmd_doctor` and nothing guarantees they agree without a
    # test that exercises both conditions together.
    (tmp_path / "config.toml").write_text('power_block = "sleep"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.path_for(tmp_path).write_text('{"held": ["sleep"], "pid":', encoding="utf-8")
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[WARN] power hold" in out
    assert "unknown" in out.lower()
    assert "holding nothing" not in out


def test_power_survives_a_non_list_held_field_without_raising(tmp_path, monkeypatch, capsys):
    # The reviewer's exact probe: `{"held": 5, ...}` used to raise
    # `TypeError: 'int' object is not iterable` out of `crr power`.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION, "want": ["sleep"], "held": 5,
                                 "reason": "r", "pid": os.getpid(), "updated": time.time()})
    rc = cli.main(["power"])  # must not raise
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out.lower()


def test_power_survives_a_string_held_field_without_iterating_its_letters(
        tmp_path, monkeypatch, capsys):
    # The reviewer's other exact probe: `{"held": "sleep"}` never raised
    # (a string IS iterable) but used to silently render as
    # "holding: e, l, p, s" -- garbage that looks like a real answer.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION, "want": ["sleep"], "held": "sleep",
                                 "reason": "r", "pid": os.getpid(), "updated": time.time()})
    rc = cli.main(["power"])  # must not raise
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out.lower()
    assert "e, l" not in out and "holding: e" not in out


# --- forged output structure via reason/held content (fix round 3, --------
# --- 2026-08-13) -------------------------------------------------------
#
# `held` items and `reason` were type-checked but never content-checked:
# a held item or reason containing a newline plus a fake "[ok  ] ..."
# line could forge doctor's checklist output, and a raw ANSI escape would
# pass straight through to the terminal. Checked here at the CLI surface
# (the actual place a user would see the forgery), on top of the
# interpret()-level tests in test_power.py.

def test_power_reason_with_a_forged_line_does_not_add_an_extra_line(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    forged_reason = "crr: 1 Claude session live\n  [ok  ] forged check \x1b[31mRED\x1b[0m"
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION, "want": ["sleep"], "held": ["sleep"],
                                 "reason": forged_reason,
                                 "pid": os.getpid(), "updated": time.time()})
    rc = cli.main(["power"])
    out = capsys.readouterr().out
    assert rc == 0
    # "holding: sleep — ..." plus "release with: ..." -- exactly two
    # lines, never a THIRD line forged out of the reason's embedded
    # newline. `splitlines()` doesn't count the trailing newline as an
    # extra element, so this is genuinely "how many lines got printed".
    assert len(out.splitlines()) == 2
    assert "\x1b" not in out
    assert "\n  [ok  ]" not in out


def test_power_held_item_with_a_forged_line_does_not_add_an_extra_line(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    forged_held = "sleep\n  [ok  ] forged\x1b[31m"
    # `want` is empty here on purpose: this test counts LINES, and a
    # `want` that the forged `held` cannot satisfy would legitimately add
    # the "NOT holding" line, hiding whether a forged one also appeared.
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION, "want": [], "held": [forged_held],
                                 "reason": "r", "pid": os.getpid(), "updated": time.time()})
    rc = cli.main(["power"])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(out.splitlines()) == 2
    assert "\x1b" not in out
    assert "\n  [ok  ]" not in out


def test_doctor_reason_with_a_forged_line_does_not_forge_a_check(
        tmp_path, monkeypatch, capsys):
    # The specific failure named by the reviewer: a forged "[ok  ] ..."
    # line inside doctor's checklist output is the same lie as a wrong
    # verdict, just with better typography.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    forged_reason = "crr: 1 Claude session live\n  [ok  ] forged check — everything fine"
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION, "want": ["sleep"], "held": ["sleep"],
                                 "reason": forged_reason,
                                 "pid": os.getpid(), "updated": time.time()})
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    # Exactly one line contains "power hold" -- the forged text is merged
    # into that SAME line (the newline that would have started a second
    # one is gone), never its own separate "[ok  ] forged check" entry.
    power_lines = [ln for ln in lines if "power hold" in ln]
    assert len(power_lines) == 1
    forged_lines = [ln for ln in lines
                    if "forged check" in ln and "power hold" not in ln]
    assert forged_lines == []


# --- the actual bug, cross-process (fix round 1, 2026-08-13) ---------------
#
# Every test above patches `cli._power_holder`/`cli._power_source` IN THIS
# SAME PROCESS, which is exactly the shape that missed the original bug:
# an in-process fake can't demonstrate that a SEPARATE process's holder is
# invisible to this one, because there was only ever one holder object the
# whole test ever touched. The review that caught this ran a real `crr
# awake` and measured a live holder-child pid while a separate `crr power`
# printed "holding: nothing". These two harnesses reproduce that exactly:
# two genuinely different `subprocess.Popen` processes, agreeing only
# through the state file on disk -- nothing shared in memory.

_AWAKE_CROSS_PROCESS_HARNESS = '''\
import pathlib
from crr import cli


class _FakeSource:
    def on_ac(self):
        return True


class _HoldingHolder:
    """Stands in for a real systemd-inhibit/caffeinate/Windows holder:
    `.held()` answers from ITS OWN in-memory state, exactly like every
    real adapter -- the property that makes it invisible cross-process."""

    def __init__(self):
        self._held = frozenset()

    def capabilities(self):
        return frozenset({{"sleep"}})

    def hold(self, want, reason):
        self._held = want
        print("hold", flush=True)

    def release(self):
        self._held = frozenset()
        print("release", flush=True)

    def held(self):
        return self._held


cli._power_holder = lambda *a, **k: _HoldingHolder()
cli._power_source = lambda *a, **k: _FakeSource()
cli.state_dir.state_dir = lambda: pathlib.Path({tmp_path!r})
cli._load_config = lambda: {{
    "power_block": "sleep",
    "power_block_requires_ac": True,
    "power_poll_seconds": {poll_seconds},
    "power_block_max_hours": 12,
    "interop_timeout_seconds": 5,
    "power_state_max_age_multiplier": 3,
}}
cli._power_entries_and_owners = lambda *a, **k: ([{{"pid": 1}}], {{1: [11]}})

cli.main(["awake"])
'''

_POWER_CROSS_PROCESS_HARNESS = '''\
import pathlib
import sys
from crr import cli


class _FakeSource:
    def on_ac(self):
        return True


class _UnusedHolder:
    """`crr power` still constructs A holder (for `.capabilities()`, used
    by `unmet`) -- but this one is never `.hold()`-ed or asked `.held()`,
    proving the reported hold came from the state file, not from here."""

    def capabilities(self):
        return frozenset({{"sleep", "shutdown"}})


cli._power_holder = lambda *a, **k: _UnusedHolder()
cli._power_source = lambda *a, **k: _FakeSource()
cli.state_dir.state_dir = lambda: pathlib.Path({tmp_path!r})
cli._load_config = lambda: {{
    "power_block": "sleep",
    "power_block_requires_ac": True,
    "power_poll_seconds": 30,
    "power_block_max_hours": 12,
    "interop_timeout_seconds": 5,
    "power_state_max_age_multiplier": 3,
}}
cli._power_entries_and_owners = lambda *a, **k: ([], {{}})

rc = cli.main(["power"])
sys.exit(rc)
'''


def test_power_sees_a_real_separate_awake_process_holding(tmp_path):
    # The actual bug, reproduced: a genuinely separate `crr awake` process
    # holding, sampled by a genuinely separate `crr power` process. Before
    # the fix this printed "holding: nothing" -- the failure an in-process
    # fake cannot exhibit, because it never has two processes to fail
    # across.
    awake_script = tmp_path / "run_awake.py"
    awake_script.write_text(_AWAKE_CROSS_PROCESS_HARNESS.format(
        tmp_path=str(tmp_path), poll_seconds=60))
    awake_proc = subprocess.Popen(
        [sys.executable, str(awake_script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output_lines = []
    try:
        # Startup-bound guards, not behavior timeouts: both children spawn a
        # fresh interpreter and import crr before doing anything. The marker
        # wait returns the instant "hold" appears, and the power sample exits
        # as soon as it has read the state, so generous ceilings only bite the
        # hang case (they were the source of intermittent CI failures at 5/10s).
        assert _wait_for_marker(awake_proc, "hold", output_lines, timeout=30), (
            f"awake child never reached its first poll; output so far: {output_lines}")

        power_script = tmp_path / "run_power.py"
        power_script.write_text(
            _POWER_CROSS_PROCESS_HARNESS.format(tmp_path=str(tmp_path)))
        power_result = subprocess.run(
            [sys.executable, str(power_script)],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        awake_proc.terminate()
        awake_proc.wait(timeout=5)

    assert power_result.returncode == 0, power_result.stdout + power_result.stderr
    out = power_result.stdout
    assert "sleep" in out
    assert "holding: nothing" not in out
    assert "unknown" not in out.lower()


# --- critical 1: a hold that was REQUESTED and FAILED (final fix wave, -----
# --- 2026-08-13) -----------------------------------------------------------
#
# `_stamp_power_state` recorded only `holder.held()`. With
# `power_block="sleep"`, a live session, and a holder that FAILS (on this
# WSL host `systemd-inhibit --mode=block` is denied outright -- "Failed to
# inhibit: Access denied", exit 1 -- for lack of a logind session), the
# writer produced `{"held": [], "reason": "no reason recorded"}` with its
# own live pid. Measured end to end before the fix: `crr power` printed
# "holding: nothing — no reason recorded" and `crr doctor` printed
# "[ok  ] power hold — holding nothing — no reason recorded". Green, with
# protection requested and none obtained.

class _FailingHolder:
    """Asked for a hold, obtains nothing, and says why -- the shape of a
    denied `systemd-inhibit` (or an unreadable logind config)."""

    def __init__(self, withheld="systemd-inhibit exited 1: Access denied",
                 caps=frozenset({"sleep", "shutdown"})):
        self._withheld = withheld
        self._caps = caps
        self.calls = []

    def capabilities(self):
        return self._caps

    def hold(self, want, reason):
        self.calls.append(("hold", want, reason))

    def release(self):
        self.calls.append(("release",))

    def held(self):
        return frozenset()

    def withheld(self):
        return self._withheld


class _PartialHolder:
    """Obtains SOME of what was asked -- the Linux holder on a host with
    `LidSwitchIgnoreInhibited=no`, which drops the sleep half."""

    def __init__(self):
        self._held = frozenset()

    def capabilities(self):
        return frozenset({"sleep", "shutdown"})

    def hold(self, want, reason):
        self._held = want - {"sleep"}

    def release(self):
        self._held = frozenset()

    def held(self):
        return self._held

    def withheld(self):
        return ("not blocking sleep: this host sets "
                "LidSwitchIgnoreInhibited=no")


class _NoWithheldHolder(_FakeHolder):
    """The macOS and Windows holders have no `withheld()` at all. Stamping
    must not crash on them (nor invent a reason they never gave)."""


def test_stamp_records_what_was_asked_for_not_only_what_was_obtained(tmp_path):
    holder = _FailingHolder()
    decision = power.decide(live_sessions=1, on_ac=True, mode="sleep",
                            requires_ac=True)
    holder.hold(decision.want, decision.reason)
    cli._stamp_power_state(tmp_path, holder, decision)
    data = power_state.read(tmp_path)
    assert data["want"] == ["sleep"]
    assert data["held"] == []


def test_stamp_records_the_holders_withheld_reason_instead_of_a_placeholder(tmp_path):
    # Important 3: `LinuxPowerHolder.withheld()` said "for doctor" and was
    # read by nothing but its own tests. The literal "no reason recorded"
    # went to disk instead, so the one explanation crr had was discarded
    # at the only point it could have reached a user.
    holder = _FailingHolder()
    decision = power.decide(live_sessions=1, on_ac=True, mode="sleep",
                            requires_ac=True)
    holder.hold(decision.want, decision.reason)
    cli._stamp_power_state(tmp_path, holder, decision)
    assert power_state.read(tmp_path)["reason"] == (
        "systemd-inhibit exited 1: Access denied")


def test_stamp_does_not_crash_on_a_holder_without_a_withheld_method(tmp_path):
    # The macOS/Windows holders don't have one. A `getattr` default, not an
    # AttributeError, and not a Protocol default method (PowerHolder is
    # structural -- nothing subclasses it, so a default body would never
    # run).
    holder = _NoWithheldHolder(caps=frozenset())
    decision = power.decide(live_sessions=1, on_ac=True, mode="sleep",
                            requires_ac=True)
    holder.hold(decision.want, decision.reason)
    cli._stamp_power_state(tmp_path, holder, decision)
    data = power_state.read(tmp_path)
    assert data["want"] == ["sleep"] and data["held"] == []


def test_stamp_does_not_reuse_a_stale_withheld_reason_when_nothing_was_asked_for(
        tmp_path):
    # `LinuxPowerHolder.hold()` clears `_withheld` on entry; `release()`
    # does NOT. The poll loop calls `release()` on every idle poll, so
    # without this guard the last hold's reason ("not blocking sleep:
    # LidSwitchIgnoreInhibited=no") is stamped onto a snapshot whose real
    # reason is "no live claude session" -- a stale explanation presented
    # as the current one.
    holder = _FailingHolder()
    holder.hold(frozenset({"sleep"}), "r")          # sets a withheld reason
    idle = power.decide(live_sessions=0, on_ac=True, mode="sleep",
                        requires_ac=True)
    assert idle.want == frozenset()
    cli._stamp_power_state(tmp_path, holder, idle)
    reason = power_state.read(tmp_path)["reason"]
    assert reason == "no live claude session"
    assert "LidSwitch" not in reason and "Access denied" not in reason


def test_power_reports_unknown_when_a_hold_was_asked_for_and_failed(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FailingHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners",
                        lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, power.snapshot(
        frozenset(), "systemd-inhibit exited 1: Access denied",
        os.getpid(), time.time(), want=frozenset({"sleep"})))
    assert cli.main(["power"]) == 0
    out = capsys.readouterr().out
    assert "holding: unknown" in out
    assert "sleep" in out                       # names what was asked for
    assert "Access denied" in out               # and why it was not obtained
    assert "holding: nothing" not in out
    assert "no reason recorded" not in out


def test_doctor_warns_when_a_hold_was_asked_for_and_failed(
        tmp_path, monkeypatch, capsys):
    (tmp_path / "config.toml").write_text('power_block = "sleep"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FailingHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners",
                        lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, power.snapshot(
        frozenset(), "systemd-inhibit exited 1: Access denied",
        os.getpid(), time.time(), want=frozenset({"sleep"})))
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "[WARN] power hold" in out
    assert "[ok  ] power hold" not in out
    assert "sleep" in out and "Access denied" in out
    assert "holding nothing" not in out


def test_power_names_the_half_of_a_partial_hold_it_did_not_obtain(
        tmp_path, monkeypatch, capsys):
    # `held` is non-empty, so the "obtained nothing" rule does NOT fire --
    # and `unmet()` cannot cover this either, because the platform CAN do
    # both (`capabilities()` is the static {"sleep","shutdown"}). Without
    # `want` on the report this rendered as an unqualified success.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _PartialHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: _POWER_CFG(power_block="sleep+shutdown"))
    monkeypatch.setattr(cli, "_power_entries_and_owners",
                        lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, power.snapshot(
        frozenset({"shutdown"}), "not blocking sleep: this host sets "
        "LidSwitchIgnoreInhibited=no", os.getpid(), time.time(),
        want=frozenset({"sleep", "shutdown"})))
    assert cli.main(["power"]) == 0
    out = capsys.readouterr().out
    assert "holding: shutdown" in out
    assert "not holding: sleep" in out.lower()
    assert "LidSwitchIgnoreInhibited=no" in out


def test_doctor_warns_on_a_partial_hold_rather_than_reporting_the_half_it_got(
        tmp_path, monkeypatch, capsys):
    (tmp_path / "config.toml").write_text(
        'power_block = "sleep+shutdown"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _PartialHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners",
                        lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, power.snapshot(
        frozenset({"shutdown"}), "not blocking sleep: this host sets "
        "LidSwitchIgnoreInhibited=no", os.getpid(), time.time(),
        want=frozenset({"sleep", "shutdown"})))
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "[WARN] power hold —" in out
    assert "shutdown" in out and "sleep" in out
    assert "LidSwitchIgnoreInhibited=no" in out


# --- minor 5: `holding: sleep — None`, and `held: ["\n"]` ------------------

def test_power_never_renders_none_as_the_reason(tmp_path, monkeypatch, capsys):
    # `interpret` maps an empty/absent `reason` to None on an otherwise
    # TRUSTED report, and the render f-stringed it straight through.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG())
    monkeypatch.setattr(cli, "_power_entries_and_owners",
                        lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION,
                                 "want": ["sleep"], "held": ["sleep"],
                                 "pid": os.getpid(), "updated": time.time()})
    assert cli.main(["power"]) == 0
    out = capsys.readouterr().out
    assert "holding: sleep" in out
    assert "None" not in out


def test_doctor_never_renders_none_as_the_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners",
                        lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION,
                                 "want": ["sleep"], "held": ["sleep"],
                                 "pid": os.getpid(), "updated": time.time()})
    assert cli.main(["doctor"]) == 0
    line = [ln for ln in capsys.readouterr().out.splitlines()
            if "power hold —" in ln][0]
    assert "None" not in line


def test_power_does_not_render_a_held_item_that_is_only_control_characters(
        tmp_path, monkeypatch, capsys):
    # `held: ["\n"]` sanitizes to `frozenset({""})` -- TRUTHY, so the
    # "something is held" branch fired and printed `holding:  — ...`: a
    # positive claim naming nothing.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda: _POWER_CFG(power_block="off"))
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([], {}))
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION,
                                 "want": [], "held": ["\n"],
                                 "reason": "power_block is off",
                                 "pid": os.getpid(), "updated": time.time()})
    assert cli.main(["power"]) == 0
    out = capsys.readouterr().out
    assert "holding: nothing" in out
    assert "holding:  " not in out


# --- important 2: --release must not erase a fresh, live, trustworthy claim -

def test_power_release_does_not_clear_a_fresh_live_claim_when_the_stop_succeeds(
        tmp_path, monkeypatch, capsys):
    # On the HEALTHY path the post-stop `_power_report` already sees a dead
    # writer, so `already_untrustworthy` covers it and the `ok` disjunct is
    # redundant. `ok` therefore only ever fired when the writer was STILL
    # ALIVE and FRESH -- exactly when clearing is wrong. Reachable whenever
    # the unit is loaded-but-inactive while a loop runs outside it (a manual
    # `crr awake`, the spec's own headless escape hatch): `systemctl stop`
    # exits 0 having stopped nothing, the file is deleted, and the next read
    # is `never_reported=True` -- "no keep-awake loop has reported", the
    # strongest false claim the type has, over a hold that is still active.
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", os.getpid(),
        time.time(), want=frozenset({"sleep"})))
    assert cli.main(["power", "--release"]) == 0
    assert power_state.read(tmp_path) is not None   # the live claim survives
    err = capsys.readouterr().err
    assert "did NOT clear" in err
    assert "live report" in err


# --- important 4: schtasks installs no keep-awake, and says so -------------

def test_schtasks_states_that_it_installs_no_keep_awake(monkeypatch, capsys):
    # README documents `crr schtasks` as the Windows/WSL install path, and
    # it emits watchdog + dashboard and no hold at all. An honest STATED
    # gap beats a half-working installer whose release path (`crr power
    # --release` -> `systemctl`) targets a unit this path never wrote.
    monkeypatch.setattr(cli, "_load_config", lambda: {
        "watchdog_interval_seconds": 60, "dashboard_port": 8377})
    assert cli.main(["schtasks"]) == 0
    out = capsys.readouterr().out
    assert "keep-awake" in out
    assert "crr awake" in out


def test_power_release_names_the_unit_it_stops(tmp_path, monkeypatch, capsys):
    # A schtasks-installed host has no crr-awake unit; a bare `systemctl`
    # error with no context is the "button that looks like it did
    # something" this command's own docstring forbids.
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    assert cli.main(["power", "--release"]) == 0
    assert "crr-awake.service" in capsys.readouterr().out


def test_power_release_explains_the_schtasks_gap_when_the_stop_fails(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: False)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    assert cli.main(["power", "--release"]) == 1
    err = capsys.readouterr().err
    assert "schtasks" in err
    assert "crr awake" in err


def test_schtasks_states_the_keep_awake_gap_on_the_install_path(monkeypatch, capsys):
    # The bare `crr schtasks` print is not where the affected user is --
    # they are at `--install`, which is the command that leaves them with
    # a watchdog, a dashboard, and no hold.
    monkeypatch.setattr(cli, "_load_config", lambda: {
        "watchdog_interval_seconds": 60, "dashboard_port": 8377})
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fake/schtasks.exe")
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    assert cli.main(["schtasks", "--install"]) == 0
    out = capsys.readouterr().out
    assert "keep-awake" in out and "crr awake" in out


def test_schtasks_states_the_keep_awake_gap_even_when_the_install_fails(
        monkeypatch, capsys):
    # A PARTIAL install is when a user is most likely to assume the
    # missing piece is the keep-awake one and retry.
    monkeypatch.setattr(cli, "_load_config", lambda: {
        "watchdog_interval_seconds": 60, "dashboard_port": 8377})
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fake/schtasks.exe")
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: False)
    assert cli.main(["schtasks", "--install"]) == 1
    out = capsys.readouterr().out
    assert "keep-awake" in out and "crr awake" in out


def test_schtasks_states_the_keep_awake_gap_on_the_uninstall_path(
        monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_config", lambda: {
        "watchdog_interval_seconds": 60, "dashboard_port": 8377})
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fake/schtasks.exe")
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    assert cli.main(["schtasks", "--uninstall"]) == 0
    out = capsys.readouterr().out
    assert "keep-awake" in out and "crr awake" in out
