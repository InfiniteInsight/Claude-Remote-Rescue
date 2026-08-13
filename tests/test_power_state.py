"""power_state adapter (fix round 1, 2026-08-13) — atomic I/O for the
awake loop's cross-process state file.

Interpretation lives in crr.core.power (interpret/snapshot); this covers
only the file plumbing: write/read round-trips, and a missing/corrupt
file both collapsing to None rather than raising or guessing.
"""

from crr.adapters import power_state


def test_write_then_read_round_trips(tmp_path):
    data = {"v": 1, "held": ["sleep"], "reason": "crr: 1 Claude session live",
            "pid": 4242, "updated": 1000.0}
    power_state.write(tmp_path, data)
    assert power_state.read(tmp_path) == data


def test_read_missing_file_is_none(tmp_path):
    assert power_state.read(tmp_path) is None


def test_read_corrupt_json_is_none_not_a_raise(tmp_path):
    power_state.path_for(tmp_path).write_text("{not json", encoding="utf-8")
    assert power_state.read(tmp_path) is None


def test_read_a_json_array_is_none_not_a_crash(tmp_path):
    # Valid JSON, wrong shape (interpret() expects a dict) -- must not
    # explode the caller, just read as "nothing trustworthy here".
    power_state.path_for(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
    assert power_state.read(tmp_path) is None


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
