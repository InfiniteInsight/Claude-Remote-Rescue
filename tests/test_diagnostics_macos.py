"""macOS diagnostics adapter tests (log + pmset).

The ``collect`` orchestration + degrade logic is tested cross-platform by
monkeypatching ``_run`` with canned command output (no Mac needed). A
Darwin-gated smoke test then asserts the REAL commands run — strictly, so
a broken invocation surfaces as a red test instead of a silent empty
``host_events`` (the swallowed-exit-code lesson). That strict smoke test is
what will validate the real ``log show`` / ``pmset`` format on macOS CI.
"""

import platform

import pytest

from crr.adapters import diagnostics_macos as mac
from crr.core import config as cfg


def _canned(argv, timeout):
    if argv[:2] == ["sysctl", "-n"]:
        return "{ sec = 1700000000, usec = 0 } Sat Nov 14 2023"
    if argv[0] == "log":
        return "2026-07-23 12:00:00 kernel panic: something bad\nnormal unrelated line\n"
    if argv[0] == "pmset":
        return "2026-07-23 12:01:00 Wake from Standby\nidle chatter\n"
    return ""


def test_collect_gathers_boots_and_host_events_but_degrades_prev_errors(monkeypatch):
    monkeypatch.setattr(mac, "available", lambda: True)
    monkeypatch.setattr(mac, "_run", _canned)
    boots, prev, events, degraded = mac.collect(cfg.Config())
    # boots = the single current-boot record from kern.boottime.
    assert boots and boots[0]["boot_id"] == "1700000000"
    # host_events pulls the death signatures from BOTH log show and pmset.
    assert any("panic" in e.lower() for e in events)
    assert any("wake" in e.lower() for e in events)
    assert "host_events" not in degraded
    # prev_boot_errors has no macOS source — degraded, never fabricated.
    assert prev == [] and "prev_boot_errors" in degraded


def test_collect_all_degraded_when_tools_absent(monkeypatch):
    monkeypatch.setattr(mac, "available", lambda: False)
    _, _, _, degraded = mac.collect(cfg.Config())
    assert set(degraded) == {"boots", "prev_boot_errors", "host_events"}


def test_collect_host_events_is_strict_log_show_failure_degrades_the_field(monkeypatch):
    # host_events combines log show + pmset; if EITHER fails the whole field
    # degrades, so a green host_events proves both sub-sources ran.
    monkeypatch.setattr(mac, "available", lambda: True)

    def _run(argv, timeout):
        if argv[0] == "log":
            raise RuntimeError("log show boom")
        return _canned(argv, timeout)

    monkeypatch.setattr(mac, "_run", _run)
    _, _, _, degraded = mac.collect(cfg.Config())
    assert "host_events" in degraded


def test_collect_boots_degrades_independently_of_host_events(monkeypatch):
    monkeypatch.setattr(mac, "available", lambda: True)

    def _run(argv, timeout):
        if argv[:2] == ["sysctl", "-n"]:
            raise RuntimeError("sysctl boom")
        return _canned(argv, timeout)

    monkeypatch.setattr(mac, "_run", _run)
    _, _, events, degraded = mac.collect(cfg.Config())
    assert "boots" in degraded
    assert "host_events" not in degraded  # events still gathered


# --- real-tool smoke + harvest (macOS only) -------------------------------

@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only")
def test_collect_runs_real_sources_on_macos():
    # Strict: the real log show + pmset must actually run, so host_events is
    # NOT degraded. prev_boot_errors always is. A wrong invocation (bad
    # predicate, timeout) degrades host_events and turns this red.
    boots, prev, events, degraded = mac.collect(cfg.Config())
    assert "host_events" not in degraded, f"log show / pmset did not run: {degraded}"
    assert "prev_boot_errors" in degraded
    assert isinstance(boots, list) and isinstance(events, list)
