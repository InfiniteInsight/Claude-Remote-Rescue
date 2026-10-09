"""Plain-English diagnostics summarizer tests (pure core).

Turns raw journald/WinEvent/OOM lines into human sentences. The mapping is
honest: each sentence is an inference from a log signature (framed as such),
severity-ordered, deduped; no match yields an explicit "looks clean" rather
than silence.
"""

from crr.core import explain


def test_oom_signature_is_summarized_first():
    lines = ["[124.0] Out of memory: Killed process 4242 (python)"]
    out = explain.summarize(lines, [])
    assert out
    assert "memory" in out[0].lower()


def test_oom_wins_severity_ordering_over_a_clean_reboot():
    lines = [
        "systemd-shutdown[1]: Rebooting.",
        "oom-killer: gfp_mask=0x...",
    ]
    out = explain.summarize(lines, [])
    # Both are surfaced, but the OOM (more severe) leads.
    assert "memory" in out[0].lower()
    assert any("shut down" in s.lower() or "reboot" in s.lower() for s in out)


def test_cron_at_reboot_noise_is_not_read_as_a_shutdown():
    # Regression: `@reboot` cron / `ua-reboot-cmds.service` lines contain
    # "reboot" but are NOT a host shutdown — they must not fabricate a verdict.
    lines = [
        "ua-reboot-cmds.service was skipped because of an unmet condition",
        "(CRON) INFO (Running @reboot jobs)",
    ]
    out = explain.summarize(lines, [])
    assert len(out) == 1 and "looks clean" in out[0].lower()


def test_unexpected_shutdown_signature():
    out = explain.summarize(["The previous system shutdown was unexpected. [6008]"], [])
    assert any("unexpected" in s.lower() for s in out)


def test_kernel_panic_reads_as_a_crash():
    out = explain.summarize(["Kernel panic - not syncing: VFS"], [])
    assert any("crash" in s.lower() or "panic" in s.lower() for s in out)


def test_watchdog_reset():
    out = explain.summarize(["watchdog did not stop!"], [])
    assert any("watchdog" in s.lower() for s in out)


def test_clean_shutdown_is_recognized():
    out = explain.summarize(["System is shutting down", "reboot"], [])
    assert len(out) == 1
    assert "shut down" in out[0].lower() or "restart" in out[0].lower()


def test_no_events_yields_an_explicit_clean_verdict_not_silence():
    out = explain.summarize([], [])
    assert len(out) == 1
    assert "clean" in out[0].lower() or "no host" in out[0].lower()


def test_summaries_are_deduped():
    # Two OOM lines produce one OOM sentence, not two.
    out = explain.summarize(
        ["Out of memory: Killed process 1", "Out of memory: Killed process 2"], []
    )
    oom = [s for s in out if "memory" in s.lower()]
    assert len(oom) == 1


def test_prev_boot_errors_are_also_scanned():
    # The signal can live in prev_boot_errors too (e.g. an OOM logged as an error).
    out = explain.summarize([], ["kernel: Out of memory: Killed process 99"])
    assert any("memory" in s.lower() for s in out)


# --- #148: the real lines from a 78 GB OOM kill, as `journalctl -k -b -1`
# printed them. The kill was replayed into the journal on every WSL distro
# restart, so the same lines recur.
OOM_KILL_JOURNAL = [
    "node invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=200",
    "oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=user.slice,mems_allowed=0,global_oom,task_memcg=/init.scope,task=node,pid=21373,uid=1000",
    "Out of memory: Killed process 21373 (node) total-vm:95301400kB, anon-rss:81837596kB, file-rss:0kB, shmem-rss:0kB, UID:1000 pgtables:189128kB oom_score_adj:0",
    "systemd[1]: init.scope: A process of this unit has been killed by the OOM killer.",
    "systemd[1]: init.scope: Failed with result 'oom-kill'.",
]


def test_oom_kill_is_reported_with_victim_name_and_rss():
    out = explain.summarize(OOM_KILL_JOURNAL, [])
    text = " ".join(out)
    assert "node" in text
    assert "21373" in text
    assert "78.0 GiB" in text  # 81837596 kB
    assert not any("looks clean" in s.lower() for s in out)


def test_replayed_oom_kill_is_reported_once_with_a_replay_count():
    out = explain.summarize(OOM_KILL_JOURNAL * 7, [])
    oom = [s for s in out if "memory" in s.lower()]
    assert len(oom) == 1
    assert oom[0].count("21373") == 1
    assert "7" in oom[0]  # logged 7 times


def test_distinct_oom_kills_are_each_reported():
    other = OOM_KILL_JOURNAL[2].replace("21373", "555").replace("(node)", "(python)")
    out = explain.summarize([*OOM_KILL_JOURNAL, other], [])
    text = " ".join(out)
    assert "21373" in text and "555" in text and "python" in text


def test_oom_without_a_parseable_kill_line_still_reports_oom():
    out = explain.summarize(["systemd[1]: init.scope: Failed with result 'oom-kill'."], [])
    assert not any("looks clean" in s.lower() for s in out)
    assert any("out-of-memory:" in s.lower() for s in out)
