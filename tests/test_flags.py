from crr.core.flags import FlagStore


def test_arm_then_read_roundtrips_the_sid(tmp_path):
    flags = FlagStore(tmp_path)
    flags.arm(42, "sid-abc")
    assert flags.read(42) == "sid-abc"


def test_read_absent_is_none(tmp_path):
    assert FlagStore(tmp_path).read(999) is None


def test_clear_is_idempotent(tmp_path):
    flags = FlagStore(tmp_path)
    flags.arm(7, "s")
    flags.clear(7)
    flags.clear(7)  # second clear must not raise
    assert flags.read(7) is None


def test_arm_overwrites_and_pids_are_isolated(tmp_path):
    flags = FlagStore(tmp_path)
    flags.arm(1, "one")
    flags.arm(1, "one-again")
    flags.arm(2, "two")
    assert flags.read(1) == "one-again"
    assert flags.read(2) == "two"
