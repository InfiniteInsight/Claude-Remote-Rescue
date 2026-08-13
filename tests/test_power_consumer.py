"""The power-block consumer: poll step, loop, and commands.

Phase 1a shipped adapters nothing called. These are the tests for the
wiring that finally calls them.
"""

import os
import select
import signal
import subprocess
import sys
import time

import pytest

from crr import cli
from crr.adapters import power_state
from crr.core import power
from crr.core.config import DEFAULTS


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


def _wait_for_marker(proc, marker, output_lines, timeout):
    """Read lines from ``proc.stdout`` until one contains ``marker``.

    Bounded by ``timeout`` via ``select`` so a hung/misbehaving child
    cannot block the test suite forever. Returns False on timeout or EOF
    (process exited without ever printing the marker) instead of raising,
    so callers get a normal assertion failure with the captured output.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            return False
        line = proc.stdout.readline()
        if line == "":
            return False  # EOF: the child exited before printing it
        output_lines.append(line.rstrip("\n"))
        if marker in line:
            return True


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
        assert _wait_for_marker(proc, "hold", output_lines, timeout=5), (
            f"child never reached its first poll; output so far: {output_lines}")
        proc.send_signal(sig)   # first stop signal, while idling between polls
        assert _wait_for_marker(proc, "release-start", output_lines, timeout=5), (
            f"child never entered release(); output so far: {output_lines}")
        time.sleep(gap)   # let release() get partway through its ladder
        proc.send_signal(sig)   # second stop signal, mid-release
        remaining = proc.stdout.read()
        if remaining:
            output_lines.extend(remaining.splitlines())
        rc = proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    return rc, output_lines


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
        frozenset({"sleep"}), "crr: 2 Claude sessions live", os.getpid(), time.time()))
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
        frozenset({"sleep"}), "r", os.getpid(), time.time()))
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
        os.getpid(), time.time()))
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
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: True)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", os.getpid(), time.time()))
    assert cli.main(["power", "--release"]) == 0
    assert power_state.read(tmp_path) is None


def test_power_release_clears_the_state_file_even_when_the_stop_command_fails(
        tmp_path, monkeypatch, capsys):
    # Fix round 2 (2026-08-13): measured with a dead-writer file present,
    # `crr power --release` against a unit that had already crashed (not
    # loaded) exited 1 and the stale file survived -- leaving `crr
    # doctor` stuck at [WARN] forever with no path back to green even
    # after the user did exactly the right recovery action. A crashed
    # loop is precisely when this file is stale and the user is trying to
    # clear it, so the clear must not be conditioned on the stop
    # command's own exit code.
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: False)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    power_state.write(tmp_path, power.snapshot(
        frozenset({"sleep"}), "crr: 1 Claude session live", os.getpid(), time.time()))
    assert cli.main(["power", "--release"]) == 1  # the stop command's own failure is still reported
    assert power_state.read(tmp_path) is None      # but the stale file is gone regardless


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
        frozenset(), "power_block is off", os.getpid(), time.time()))
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
        frozenset({"sleep"}), "crr: 1 Claude session live", 0, time.time()))
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
        frozenset({"sleep"}), "crr: 1 Claude session live", dead.pid, time.time()))
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
        os.getpid(), time.time() - 1000))  # far past 10 * 3 = 30s
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
        frozenset({"sleep"}), "crr: 1 Claude session live", os.getpid(), time.time()))
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
        frozenset({"sleep"}), "crr: 1 Claude session live", dead.pid, time.time()))
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
        frozenset(), "no live claude session", os.getpid(), time.time()))
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
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION, "held": 5,
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
    power_state.write(tmp_path, {"v": power.POWER_SNAPSHOT_VERSION, "held": "sleep",
                                 "reason": "r", "pid": os.getpid(), "updated": time.time()})
    rc = cli.main(["power"])  # must not raise
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out.lower()
    assert "e, l" not in out and "holding: e" not in out


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
        assert _wait_for_marker(awake_proc, "hold", output_lines, timeout=5), (
            f"awake child never reached its first poll; output so far: {output_lines}")

        power_script = tmp_path / "run_power.py"
        power_script.write_text(
            _POWER_CROSS_PROCESS_HARNESS.format(tmp_path=str(tmp_path)))
        power_result = subprocess.run(
            [sys.executable, str(power_script)],
            capture_output=True, text=True, timeout=10,
        )
    finally:
        awake_proc.terminate()
        awake_proc.wait(timeout=5)

    assert power_result.returncode == 0, power_result.stdout + power_result.stderr
    out = power_result.stdout
    assert "sleep" in out
    assert "holding: nothing" not in out
    assert "unknown" not in out.lower()
