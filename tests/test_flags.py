from crr.core.flags import FlagStore, RELAUNCH, CLOSE


def test_arm_relaunch_roundtrips_kind_and_sid(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_relaunch(42, "sid-abc")
    assert f.read(42) == (RELAUNCH, "sid-abc")


def test_arm_close_roundtrips_kind_with_no_sid(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_close(7)
    assert f.read(7) == (CLOSE, None)


def test_read_absent_is_none(tmp_path):
    assert FlagStore(tmp_path).read(999) is None


def test_clear_is_idempotent(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_close(7)
    f.clear(7)
    f.clear(7)  # second clear must not raise
    assert f.read(7) is None


def test_arm_overwrites_and_pids_are_isolated(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_relaunch(1, "one")
    f.arm_close(1)              # overwrite pid 1 with a different kind
    f.arm_relaunch(2, "two")
    assert f.read(1) == (CLOSE, None)
    assert f.read(2) == (RELAUNCH, "two")
