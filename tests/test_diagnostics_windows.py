"""Windows/WSL diagnostics parser tests (Phase 4).

Pure parsers, tested with synthetic command output — the verifiable core of
the Windows/WSL diagnostics source. The [lesson: the 90GB that nobody owned]
is encoded directly: OOM forensics must surface inactive_anon/shmem, not
just process RSS. The real powershell/dmesg round-trip is author-verified on
Windows (task #8's OOM replay); wiring into gather_diagnostics lands with
the platform-dispatch refactor (PR #11).
"""

from crr.adapters import diagnostics_windows as dw


def test_winevent_command_filters_the_shutdown_ids():
    cmd = dw.winevent_command((1074, 6008, 41), cap=50)
    assert cmd[0] == "powershell.exe"
    joined = " ".join(cmd)
    assert "Get-WinEvent" in joined
    assert "1074,6008,41" in joined
    assert "50" in joined  # MaxEvents cap


def test_parse_winevents_keeps_nonblank_lines():
    text = "2026-07-20 [6008] The previous system shutdown was unexpected.\n\n  \n2026-07-21 [41] Kernel-Power\n"
    assert dw.parse_winevents(text) == [
        "2026-07-20 [6008] The previous system shutdown was unexpected.",
        "2026-07-21 [41] Kernel-Power",
    ]


def test_parse_oom_lines_catches_the_killer_and_victims():
    dmesg = (
        "[123.4] normal message\n"
        "[124.0] Out of memory: Killed process 4242 (python) total-vm:90G\n"
        "[124.1] oom-killer: gfp_mask=0x...\n"
        "[125.0] unrelated\n"
    )
    hits = dw.parse_oom_lines(dmesg)
    assert any("Killed process 4242" in h for h in hits)
    assert any("oom-killer" in h for h in hits)
    assert len(hits) == 2


def test_parse_meminfo_reads_kb_fields():
    mem = dw.parse_meminfo(
        "MemTotal:       16000000 kB\n"
        "MemAvailable:     200000 kB\n"
        "Shmem:          90000000 kB\n"
        "Inactive(anon):  8000000 kB\n"
    )
    assert mem["MemTotal"] == 16000000
    assert mem["Shmem"] == 90000000
    assert mem["Inactive(anon)"] == 8000000


def test_memory_forensics_surfaces_shmem_and_anon_not_just_rss():
    # The lesson: the killer's victims are usually bystanders — shmem/tmpfs and
    # inactive_anon, which never show up in a per-process RSS view.
    mem = {"MemTotal": 16777216, "MemAvailable": 102400,
           "Shmem": 94371840, "Inactive(anon)": 8388608}
    line = dw.format_memory_forensics(mem)
    assert "Shmem" in line
    assert "Inactive(anon)" in line
    # rendered as human sizes, not raw kB
    assert "GiB" in line or "MiB" in line


def test_memory_forensics_empty_when_fields_absent():
    assert dw.format_memory_forensics({}) == ""
