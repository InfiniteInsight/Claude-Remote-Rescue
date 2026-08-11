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
