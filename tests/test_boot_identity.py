"""Boot-identity adapter tests.

The Linux and macOS ``current()`` methods touch the OS, so the value-
extraction logic is factored into pure parsers tested with synthetic
input. ``detect()`` selection is asserted per platform (gated).
"""

import os
import platform

import pytest

from crr.adapters import boot_identity

# The rest of the suite runs against a stand-in on platforms crr has no
# adapter for (see tests/conftest.py). This file is where the truth about
# detect() is asserted, so it opts out — otherwise the stub would answer
# the very question these tests exist to ask.
pytestmark = pytest.mark.real_boot_identity


# --- macOS boottime parsing (pure) ---------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("{ sec = 1784723478, usec = 0 } Wed Jul 23 15:20:00 2026", "1784723478"),
    ("{ sec = 42, usec = 999 } some date", "42"),
    ("{sec=100,usec=0}", "100"),  # no spaces
])
def test_parse_boottime_extracts_seconds(raw, expected):
    assert boot_identity._parse_boottime(raw) == expected


def test_parse_boottime_rejects_unparseable():
    with pytest.raises(ValueError):
        boot_identity._parse_boottime("no seconds here")


# --- detect() selection ---------------------------------------------------

def test_detect_matches_the_running_platform():
    system = platform.system()
    if system == "Linux":
        assert isinstance(boot_identity.detect(), boot_identity.LinuxBootIdentity)
    elif system == "Darwin":
        assert isinstance(boot_identity.detect(), boot_identity.MacBootIdentity)
    else:
        with pytest.raises(NotImplementedError):
            boot_identity.detect()


@pytest.mark.skipif(os.name != "nt", reason="asserts what Windows actually does")
def test_windows_has_no_boot_identity_adapter_yet():
    # Named separately from the generic platform test because the rest of
    # the suite runs against a stand-in here, and a stand-in that nothing
    # contradicts is how a gap goes quiet. This is the contradiction: crr
    # cannot classify a session on Windows, every command exits 2, and that
    # stays measured until #75 lands — at which point this test fails and
    # tells its replacement where to look.
    with pytest.raises(NotImplementedError) as excinfo:
        boot_identity.detect()
    assert "Windows" in str(excinfo.value)


def test_current_boot_id_is_stable_and_nonempty():
    # Whatever platform we're on, a supported adapter returns a stable,
    # non-empty identity (two reads within one boot agree).
    try:
        adapter = boot_identity.detect()
    except NotImplementedError:
        pytest.skip("no boot-identity adapter for this platform")
    first = adapter.current()
    assert first and first == adapter.current()


# --- Linux: distro-lifetime identity (#138) -------------------------------
#
# A WSL distro restart keeps the VM kernel (and its boot_id) but restarts
# PID 1, so PID 1's start time (field 22 of /proc/1/stat, clock ticks since
# kernel boot) is what tells two distro lives apart.

_STAT = "1 (systemd) S 0 1 1 0 -1 4194560 " + " ".join(["0"] * 12) + " 12345 0 0"  # field 22


def test_parse_init_start_reads_field_22():
    assert boot_identity._parse_init_start(_STAT) == "12345"


def test_parse_init_start_survives_a_comm_with_spaces_and_parens():
    stat = _STAT.replace("(systemd)", "(my (odd) init)")
    assert boot_identity._parse_init_start(stat) == "12345"


def test_parse_init_start_rejects_garbage():
    with pytest.raises(ValueError):
        boot_identity._parse_init_start("not a stat line")


def test_linux_identity_composes_kernel_id_and_init_start(tmp_path):
    kid = tmp_path / "boot_id"
    kid.write_text("246dbdec-2257-41b7-8fea-e8e719c41c0a\n")
    stat = tmp_path / "stat"
    stat.write_text(_STAT + "\n")
    ident = boot_identity.LinuxBootIdentity(boot_id_path=kid, init_stat_path=stat)
    assert ident.current() == "246dbdec-2257-41b7-8fea-e8e719c41c0a@12345"


def test_linux_identity_degrades_to_the_bare_kernel_id(tmp_path):
    # An unreadable /proc/1/stat (hidepid, odd container) must not take crr
    # down: the bare kernel id is exactly the pre-#138 identity, which
    # same_boot() still compares correctly against compound ids.
    kid = tmp_path / "boot_id"
    kid.write_text("246dbdec-2257-41b7-8fea-e8e719c41c0a\n")
    ident = boot_identity.LinuxBootIdentity(
        boot_id_path=kid, init_stat_path=tmp_path / "missing")
    assert ident.current() == "246dbdec-2257-41b7-8fea-e8e719c41c0a"
