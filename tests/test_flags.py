from crr.core.flags import FlagStore, RELAUNCH, CLOSE


def test_arm_relaunch_roundtrips_kind_and_sid(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_relaunch(42, "sid-abc", boot_id="boot-A")
    assert f.read(42) == (RELAUNCH, "sid-abc", "boot-A", False)


def test_arm_close_roundtrips_kind_with_no_sid(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_close(7, boot_id="boot-A")
    assert f.read(7) == (CLOSE, None, "boot-A", False)


def test_read_absent_is_none(tmp_path):
    assert FlagStore(tmp_path).read(999) is None


def test_clear_is_idempotent(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_close(7, boot_id="boot-A")
    f.clear(7)
    f.clear(7)  # second clear must not raise
    assert f.read(7) is None


def test_arm_overwrites_and_pids_are_isolated(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_relaunch(1, "one", boot_id="boot-A")
    f.arm_close(1, boot_id="boot-A")
    f.arm_relaunch(2, "two", boot_id="boot-A")
    assert f.read(1) == (CLOSE, None, "boot-A", False)
    assert f.read(2) == (RELAUNCH, "two", "boot-A", False)


def test_boot_id_is_preserved_in_roundtrip(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_close(42, boot_id="abc-123")
    assert f.read(42)[2] == "abc-123"
    f.arm_relaunch(42, "sid-x", boot_id="def-456")
    assert f.read(42)[2] == "def-456"


def test_legacy_flag_without_boot_id_reads_as_none(tmp_path):
    f = FlagStore(tmp_path)
    flag_dir = tmp_path / "relaunch"
    flag_dir.mkdir(parents=True, exist_ok=True)
    (flag_dir / "99").write_text("close", encoding="utf-8")
    result = f.read(99)
    assert result == (CLOSE, None, None, False)


def test_skip_permissions_roundtrips(tmp_path):
    f = FlagStore(tmp_path)
    f.arm_relaunch(42, "sid-x", boot_id="boot-A", skip_permissions=True)
    assert f.read(42) == (RELAUNCH, "sid-x", "boot-A", True)
