"""journald diagnostics source tests (#148) — journalctl faked at ``_run``."""

import re

from crr.adapters import diagnostics as jd
from crr.core import config as cfg

from tests.test_explain import NOISE, OOM_KILL_JOURNAL


def test_journalctl_grep_pattern_ignores_reboot_substring_noise():
    pattern = re.compile(jd._HOST_EVENT_PATTERN, re.I)
    assert [bool(pattern.search(line)) for line in NOISE] == [False, False, False]


def test_journalctl_grep_pattern_still_matches_real_signatures():
    pattern = re.compile(jd._HOST_EVENT_PATTERN, re.I)
    for line in (*OOM_KILL_JOURNAL, "systemd-shutdown[1]: Rebooting.",
                 "systemd[1]: Shutting down.", "watchdog did not stop!",
                 "systemd[1]: Reached target Reboot."):
        assert pattern.search(line), line


def test_host_events_returns_only_classified_lines_and_does_not_cap_them(monkeypatch):
    # The journalctl -n is a SCAN bound (diagnose_scan_cap), not the event cap:
    # capping before classifying is what let noise crowd out the OOM (#148).
    seen = {}
    out = "\n".join([*NOISE, *OOM_KILL_JOURNAL, *NOISE]) + "\n"

    def fake_run(args, timeout):
        seen["args"] = args
        return out

    monkeypatch.setattr(jd, "_run", fake_run)
    events = jd.host_events(1, 4321, 5)
    assert events == OOM_KILL_JOURNAL
    assert seen["args"][seen["args"].index("-n") + 1] == "4321"


def test_collect_scans_with_the_configured_scan_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(jd, "available", lambda: True)
    monkeypatch.setattr(jd, "_run", lambda args, timeout: calls.append(args) or "")
    jd.collect(cfg.Config())
    host = [a for a in calls if "-g" in a][0]
    assert host[host.index("-n") + 1] == str(cfg.DEFAULTS["diagnose_scan_cap"])
