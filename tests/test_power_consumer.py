"""The power-block consumer: poll step, loop, and commands.

Phase 1a shipped adapters nothing called. These are the tests for the
wiring that finally calls them.
"""

import os
import signal
import subprocess
import sys
import threading

import pytest

from crr import cli
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


def test_awake_finishes_release_even_when_a_second_sigterm_lands_mid_release(
    tmp_path, monkeypatch
):
    # A second stop signal landing WHILE release() is running (double
    # Ctrl-C, `systemctl restart` re-sending SIGTERM mid-teardown) must
    # not abort the release with the hold's handle already gone. Release
    # ladders run up to 15s (systemd-inhibit teardown, the Windows
    # child's stop sequence) -- long enough for a second signal to be
    # realistic. Fixed by ignoring SIGTERM for the duration of the
    # release path specifically, rather than leaving whatever handler
    # preceded ours in place -- that is usually SIG_DFL (instant kill),
    # which is the exact bug this guards against.
    release_started = threading.Event()
    release_done = threading.Event()
    calls = []

    class _SlowReleaseHolder:
        def capabilities(self):
            return frozenset({"sleep"})

        def hold(self, want, reason):
            calls.append(("hold", want, reason))

        def release(self):
            calls.append(("release-start",))
            release_started.set()
            # A stand-in for a slow teardown ladder. Deliberately not
            # time.sleep: this test also patches cli.time.sleep, and
            # cli.time IS the real `time` module object (same module in
            # sys.modules) -- patching cli.time.sleep patches time.sleep
            # everywhere, including here, which would turn this "sleep"
            # into another SIGTERM-sender instead of a wait.
            threading.Event().wait(0.5)
            calls.append(("release-done",))
            release_done.set()

        def held(self):
            return frozenset()

    holder = _SlowReleaseHolder()
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
            os.kill(os.getpid(), signal.SIGTERM)   # first stop signal

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    def send_second_sigterm():
        if release_started.wait(timeout=2):
            os.kill(os.getpid(), signal.SIGTERM)   # second, mid-release

    sender = threading.Thread(target=send_second_sigterm, daemon=True)
    sender.start()
    try:
        rc = cli.main(["awake"])
    finally:
        sender.join(timeout=2)
        # Belt-and-braces, same reasoning as the other SIGTERM test.
        signal.signal(signal.SIGTERM, previous_handler)

    assert rc == 0
    assert release_done.is_set()
    assert calls[-1] == ("release-done",), calls
