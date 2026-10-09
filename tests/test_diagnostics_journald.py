"""journald diagnostics source tests (#148) — journalctl faked at ``_run``."""

import re

from crr.adapters import diagnostics as jd
from crr.core import config as cfg

from test_explain import NOISE, OOM_KILL_JOURNAL  # tests/ is on sys.path (no __init__.py)


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


_START = "2026-10-09T10:00:00+0000 host systemd[1]: Startup finished in 1.2s."


def test_system_starts_returns_only_the_pid1_manager_starts(monkeypatch):
    # WSL2: a distro restart re-runs systemd (PID 1) inside the SAME kernel
    # boot, so the starts are the only trace of a restart loop. User managers
    # (systemd[<pid>]) print the same message and must not be counted.
    user = "2026-10-09T10:01:00+0000 host systemd[4821]: Startup finished in 90ms."
    monkeypatch.setattr(jd, "_run", lambda args, timeout: f"{_START}\n{user}\n")
    assert jd.system_starts(1, 5000, 5) == [_START]


def test_collect_appends_system_starts_to_host_events(monkeypatch):
    monkeypatch.setattr(jd, "available", lambda: True)
    monkeypatch.setattr(jd, "_run", lambda args, timeout: f"{_START}\n" if "-t" in args else "")
    _boots, _prev, events, degraded = jd.collect(cfg.Config())
    assert events == [_START] and degraded == []


def test_a_failing_start_query_is_degraded_not_silently_omitted(monkeypatch):
    def fake_run(args, timeout):
        if "-t" in args:
            raise RuntimeError("journalctl exited 1")
        return ""

    monkeypatch.setattr(jd, "available", lambda: True)
    monkeypatch.setattr(jd, "_run", fake_run)
    _boots, _prev, _events, degraded = jd.collect(cfg.Config())
    assert "system_starts" in degraded
    assert "host_events" not in degraded


def test_collect_scans_with_the_configured_scan_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(jd, "available", lambda: True)
    monkeypatch.setattr(jd, "_run", lambda args, timeout: calls.append(args) or "")
    jd.collect(cfg.Config())
    host = [a for a in calls if "-g" in a][0]
    assert host[host.index("-n") + 1] == str(cfg.DEFAULTS["diagnose_scan_cap"])
