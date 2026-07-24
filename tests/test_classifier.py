"""Classifier tests (live / ghost / crashed).

Pure core, driven by fake ports — no subprocesses, no real pids. The
fakes let us assert the ordering the audit cares about: a boot-identity
mismatch must decide ``crashed`` WITHOUT consulting the pid at all, or a
reboot-recycled pid gets treated as the original session
([lesson: recycled pids]).
"""

from crr.core import contracts
from crr.core.classifier import classify


_SAME_BOOT = "b8f3c0de-0000-4000-8000-000000000000"


def _entry(boot_id=_SAME_BOOT, pid=12345):
    return {
        "v": 1,
        "pid": pid,
        "boot_id": boot_id,
        "cwd": "/home/u/project",
        "host": "tmux",
        "shell": "zsh",
        "claude": {
            "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "sid_source": "injected",
            "started": "2026-07-23T00:00:00Z",
        },
        "last_cmd": "claude",
        "tmux_session": None,
        "updated": "2026-07-23T00:00:00Z",
    }


class FakeBoot:
    def __init__(self, boot):
        self._boot = boot

    def current(self):
        return self._boot


class FakeProbe:
    def __init__(self, alive, tty):
        self._alive = alive
        self._tty = tty
        self.alive_calls = []
        self.tty_calls = []

    def is_alive(self, pid):
        self.alive_calls.append(pid)
        return self._alive

    def has_controlling_tty(self, pid):
        self.tty_calls.append(pid)
        return self._tty


def test_same_boot_alive_with_tty_is_live():
    boot = FakeBoot(_SAME_BOOT)
    probe = FakeProbe(alive=True, tty=True)
    assert classify(_entry(), boot, probe) == "live"


def test_same_boot_alive_without_tty_is_ghost():
    boot = FakeBoot(_SAME_BOOT)
    probe = FakeProbe(alive=True, tty=False)
    assert classify(_entry(), boot, probe) == "ghost"


def test_same_boot_dead_pid_is_crashed():
    boot = FakeBoot(_SAME_BOOT)
    probe = FakeProbe(alive=False, tty=False)
    assert classify(_entry(), boot, probe) == "crashed"


def test_boot_mismatch_is_crashed():
    boot = FakeBoot("a-different-boot-id")
    probe = FakeProbe(alive=True, tty=True)
    assert classify(_entry(), boot, probe) == "crashed"


def test_boot_mismatch_does_not_probe_the_pid():
    # The recycled-pid guard: a stale boot means the pid is meaningless, so
    # the classifier must not even ask whether it is alive.
    boot = FakeBoot("a-different-boot-id")
    probe = FakeProbe(alive=True, tty=True)
    classify(_entry(pid=777), boot, probe)
    assert probe.alive_calls == []
    assert probe.tty_calls == []


def test_result_is_always_a_contract_state():
    boot = FakeBoot(_SAME_BOOT)
    for alive in (True, False):
        for tty in (True, False):
            result = classify(_entry(), boot, FakeProbe(alive=alive, tty=tty))
            assert result in contracts.STATES
