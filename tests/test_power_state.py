"""power_state adapter (fix round 1, 2026-08-13; ABSENT vs UNREADABLE
distinguished in fix round 2, 2026-08-13) — atomic I/O for the awake
loop's cross-process state file.

Interpretation lives in crr.core.power (interpret/snapshot); this covers
only the file plumbing: write/read round-trips, and the three-way split
between "no file" (None), "file exists but can't be trusted" (UNREADABLE),
and a real parsed dict.

Round 1 originally collapsed missing and corrupt to the SAME `None` —
measured wrong in round 2: with a hold genuinely active, truncating
power.json made a reader read the corrupt file exactly like one that had
never existed, and report "holding: nothing" while the hold was real.
`test_read_corrupt_json_is_none_not_a_raise` and
`test_read_a_json_array_is_none_not_a_crash` below are the two tests that
pinned the wrong behavior; both are rewritten here (not just the module
they cover) to assert the new distinction, keeping their original intent
(no crash) while fixing the assertion that was wrong.
"""

from crr.adapters import power_state
from crr.core.power import UNREADABLE


def test_write_then_read_round_trips(tmp_path):
    data = {"v": 1, "held": ["sleep"], "reason": "crr: 1 Claude session live",
            "pid": 4242, "updated": 1000.0}
    power_state.write(tmp_path, data)
    assert power_state.read(tmp_path) == data


def test_read_missing_file_is_none(tmp_path):
    # Genuinely absent -- no loop has EVER written anything. A KNOWN
    # nothing, distinct from UNREADABLE below.
    assert power_state.read(tmp_path) is None


def test_read_corrupt_json_is_unreadable_not_none(tmp_path):
    # Was `test_read_corrupt_json_is_none_not_a_raise`. The "no raise"
    # half of its intent is unchanged (still asserted implicitly: this
    # call would raise if `read` didn't catch the parse error); the "is
    # None" half was the bug -- a corrupt file might be hiding a real,
    # currently active hold, so it must render as UNKNOWN downstream, not
    # be indistinguishable from a file that never existed.
    power_state.path_for(tmp_path).write_text("{not json", encoding="utf-8")
    result = power_state.read(tmp_path)
    assert result is UNREADABLE
    assert result is not None


def test_read_a_json_array_is_unreadable_not_none(tmp_path):
    # Was `test_read_a_json_array_is_none_not_a_crash`. Valid JSON, wrong
    # shape (interpret() expects a dict) -- must not explode the caller,
    # and must not silently agree with "no file at all" either.
    power_state.path_for(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
    result = power_state.read(tmp_path)
    assert result is UNREADABLE
    assert result is not None


def test_read_an_empty_file_is_unreadable_not_none(tmp_path):
    # A truncated write (crash mid-write, despite the atomic rename —
    # e.g. a 0-byte target from an external tool, or an interrupted
    # non-atomic edit) is empty, not JSON, and not a missing file either.
    power_state.path_for(tmp_path).write_text("", encoding="utf-8")
    assert power_state.read(tmp_path) is UNREADABLE


def test_clear_removes_the_file(tmp_path):
    power_state.write(tmp_path, {"v": 1})
    power_state.clear(tmp_path)
    assert power_state.read(tmp_path) is None
    assert not power_state.path_for(tmp_path).exists()


def test_clear_is_idempotent_on_a_missing_file(tmp_path):
    power_state.clear(tmp_path)  # must not raise
    power_state.clear(tmp_path)


def test_write_is_atomic_no_tmp_file_left_behind(tmp_path):
    power_state.write(tmp_path, {"v": 1, "held": []})
    leftovers = list(tmp_path.glob(".*.tmp"))
    assert leftovers == []
