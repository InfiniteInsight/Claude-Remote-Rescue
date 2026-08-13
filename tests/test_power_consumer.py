"""The power-block consumer: poll step, loop, and commands.

Phase 1a shipped adapters nothing called. These are the tests for the
wiring that finally calls them.
"""

import os

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
