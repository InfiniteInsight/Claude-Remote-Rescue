"""Boot-identity adapter tests.

The Linux and macOS ``current()`` methods touch the OS, so the value-
extraction logic is factored into pure parsers tested with synthetic
input. ``detect()`` selection is asserted per platform (gated).
"""

import platform

import pytest

from crr.adapters import boot_identity


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


def test_current_boot_id_is_stable_and_nonempty():
    # Whatever platform we're on, a supported adapter returns a stable,
    # non-empty identity (two reads within one boot agree).
    try:
        adapter = boot_identity.detect()
    except NotImplementedError:
        pytest.skip("no boot-identity adapter for this platform")
    first = adapter.current()
    assert first and first == adapter.current()
