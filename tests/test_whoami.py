"""`crr whoami` — which crr session am I running inside?

Asked from Claude mobile ("run crr whoami"), the answer lands in the
conversation, which is how a user ties a mobile conversation to a card.
The ancestry walk is pure so it is testable without real processes.
"""

from crr.core import whoami


def _chain(pairs):
    """parent_of(pid) from a {child: parent} map, None past the root."""
    return lambda pid: pairs.get(pid)


def test_finds_the_nearest_journaled_ancestor():
    # Real shape, measured: python -> bash -> claude --resume -> fish(journaled)
    parent = _chain({1986856: 1986836, 1986836: 1727291, 1727291: 916, 916: 1})
    assert whoami.journaled_ancestor(1986856, {916, 1234}, parent) == 916


def test_returns_none_when_no_ancestor_is_journaled():
    parent = _chain({50: 40, 40: 30, 30: 1})
    assert whoami.journaled_ancestor(50, {999}, parent) is None


def test_matches_the_starting_pid_itself():
    assert whoami.journaled_ancestor(916, {916}, _chain({})) == 916


def test_stops_at_the_root_without_looping_forever():
    # A cycle (or a pid that parents itself) must not hang the command.
    parent = _chain({7: 7})
    assert whoami.journaled_ancestor(7, {999}, parent) is None


def test_gives_up_after_a_bounded_number_of_hops():
    # A pathological deep chain must terminate.
    parent = _chain({i: i + 1 for i in range(0, 500)})
    assert whoami.journaled_ancestor(0, {9999}, parent) is None
