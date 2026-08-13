"""Power adapters (spec 2026-08-12).

The AC probe is measured, not assumed: WSL2 passes the host battery
through sysfs (`/sys/class/power_supply/AC1/online`), which is why ONE
Linux adapter serves both native Linux and WSL.
"""

import re
import subprocess as _sp
import sys as _sys
from pathlib import Path

import pytest

from crr.adapters.power_source import (MacPowerSource, SysfsPowerSource,
                                       _parse_pmset)
from crr.adapters.power_hold_linux import (LinuxPowerHolder, inhibit_argv,
                                           lid_is_exempt)
from crr.adapters.power_hold_macos import MacPowerHolder, caffeinate_argv
from crr.adapters.power_hold_windows import (WindowsPowerHolder,
                                             holder_argv, holder_script)


def _supply(root: Path, name: str, **files: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for key, value in files.items():
        (d / key).write_text(value + "\n", encoding="utf-8")


def test_mains_online_reads_as_on_ac(tmp_path):
    _supply(tmp_path, "AC1", type="Mains", online="1")
    _supply(tmp_path, "BAT1", type="Battery", status="Full")
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_mains_offline_reads_as_on_battery(tmp_path):
    _supply(tmp_path, "AC1", type="Mains", online="0")
    _supply(tmp_path, "BAT1", type="Battery", status="Discharging")
    assert SysfsPowerSource(tmp_path).on_ac() is False


def test_a_machine_with_no_power_supplies_is_a_desktop_not_an_unknown(tmp_path):
    # Known True, not None: a desktop is always on mains. Returning None
    # here would withhold the hold on every server and every VM.
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_a_battery_with_no_mains_device_falls_back_to_its_status(tmp_path):
    _supply(tmp_path, "BAT0", type="Battery", status="Discharging")
    assert SysfsPowerSource(tmp_path).on_ac() is False
    _supply(tmp_path, "BAT0", type="Battery", status="Charging")
    assert SysfsPowerSource(tmp_path).on_ac() is True


def test_an_unreadable_probe_is_unknown_not_a_guess(tmp_path):
    _supply(tmp_path, "AC1", type="Mains")   # no `online` file at all
    assert SysfsPowerSource(tmp_path).on_ac() is None


def test_a_missing_root_is_unknown(tmp_path):
    assert SysfsPowerSource(tmp_path / "nope").on_ac() is None


def test_a_device_whose_type_is_unreadable_is_unknown_not_mains(tmp_path):
    import os
    # Skip if running as root (root ignores chmod 000)
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    # Create a device directory with an unreadable type file
    d = tmp_path / "AC1"
    d.mkdir(parents=True, exist_ok=True)
    type_file = d / "type"
    type_file.write_text("Mains\n", encoding="utf-8")
    type_file.chmod(0o000)
    # Should return None (unknown), not True (guessing mains)
    assert SysfsPowerSource(tmp_path).on_ac() is None


@pytest.mark.parametrize("text,expected", [
    ("Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged", True),
    ("Now drawing from 'Battery Power'\n -InternalBattery-0 82%", False),
    ("something unparseable", None),
    ("", None),
])
def test_pmset_parsing(text, expected):
    assert _parse_pmset(text) is expected


def test_inhibit_asks_for_sleep_not_idle():
    # `idle` inhibits logind's IdleAction, which defaults to `ignore` — it
    # would hold successfully and protect NOTHING. `sleep` is what GNOME
    # and KDE's idle-suspend actually goes through. This is the opposite
    # of the obvious choice; see the spec before "fixing" it.
    argv = inhibit_argv(frozenset({"sleep"}), "crr: 2 Claude sessions live")
    what = argv[argv.index("--what") + 1] if "--what" in argv else ""
    joined = " ".join(argv)
    assert "sleep" in what, f"must inhibit sleep, got {joined}"
    assert "idle" not in what, (
        "idle inhibits IdleAction, which defaults to ignore — a hold that "
        f"protects nothing. Got {joined}")


def test_inhibit_adds_shutdown_only_when_asked():
    both = inhibit_argv(frozenset({"sleep", "shutdown"}), "r")
    what = both[both.index("--what") + 1]
    assert set(what.split(":")) == {"sleep", "shutdown"}


def test_inhibit_is_block_mode_and_carries_the_reason():
    argv = inhibit_argv(frozenset({"sleep"}), "crr: 1 Claude session live")
    assert argv[0] == "systemd-inhibit"
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "block"
    assert "crr: 1 Claude session live" in argv


@pytest.mark.parametrize("conf,exempt", [
    ("", True),                                    # unset -> default yes
    ("#LidSwitchIgnoreInhibited=yes\n", True),     # commented -> default
    ("LidSwitchIgnoreInhibited=yes\n", True),
    ("LidSwitchIgnoreInhibited=no\n", False),
    ("[Login]\nLidSwitchIgnoreInhibited = no\n", False),
])
def test_lid_exemption_is_read_not_assumed(conf, exempt):
    assert lid_is_exempt(conf) is exempt


def test_holder_refuses_to_block_sleep_when_the_lid_is_not_exempt(tmp_path):
    # The builder's hard requirement is that closing the lid always
    # sleeps. On a host that has turned the default off, a sleep lock
    # would break that, so crr withholds instead.
    conf = tmp_path / "logind.conf"
    conf.write_text("LidSwitchIgnoreInhibited=no\n", encoding="utf-8")
    spawned = []
    holder = LinuxPowerHolder(logind_conf=conf,
                              spawn=lambda argv, **kw: spawned.append(argv))
    holder.hold(frozenset({"sleep"}), "r")
    assert spawned == [], "blocked sleep on a host where that blocks the lid"
    assert holder.held() == frozenset()


def test_holder_still_blocks_shutdown_when_the_lid_is_not_exempt(tmp_path):
    # Only the sleep half is unsafe there; shutdown is unaffected by lid
    # handling, so withholding it too would be over-correction.
    conf = tmp_path / "logind.conf"
    conf.write_text("LidSwitchIgnoreInhibited=no\n", encoding="utf-8")
    spawned = []

    class _P:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    def _spawn(argv, **kw):
        spawned.append(argv)
        return _P()

    holder = LinuxPowerHolder(logind_conf=conf, spawn=_spawn)
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"shutdown"})
    what = spawned[0][spawned[0].index("--what") + 1]
    assert what == "shutdown"


def test_capabilities_are_both_on_linux(tmp_path):
    holder = LinuxPowerHolder(logind_conf=tmp_path / "absent.conf")
    assert holder.capabilities() == frozenset({"sleep", "shutdown"})


def test_macos_can_hold_sleep_but_not_shutdown():
    # Not an omission. A launch daemon cannot block a macOS shutdown at
    # all: the cancellable notifications do not reach daemons, and only a
    # GUI app in the login session can delay one. Deferred by the spec.
    assert MacPowerHolder().capabilities() == frozenset({"sleep"})


def test_caffeinate_holds_idle_only_so_the_lid_still_sleeps():
    argv = caffeinate_argv()
    assert argv[0] == "caffeinate"
    assert "-i" in argv
    assert "-s" not in argv, "-s would fight the lid; idle only"


def test_macos_holder_ignores_a_shutdown_request_it_cannot_serve():
    spawned = []

    class _P:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    holder = MacPowerHolder(spawn=lambda argv, **kw: spawned.append(argv) or _P())
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"sleep"})
    assert len(spawned) == 1


def test_windows_claims_both_capabilities():
    assert WindowsPowerHolder().capabilities() == frozenset(
        {"sleep", "shutdown"})


def test_script_sets_execution_state_for_sleep():
    s = holder_script(frozenset({"sleep"}), "crr: 1 Claude session live")
    assert "SetThreadExecutionState" in s
    assert "0x80000001" in s or ("ES_CONTINUOUS" in s and "ES_SYSTEM_REQUIRED" in s)
    assert "ShutdownBlockReasonCreate" not in s


def test_script_registers_a_block_reason_for_shutdown():
    s = holder_script(frozenset({"sleep", "shutdown"}), "crr: 2 live")
    assert "ShutdownBlockReasonCreate" in s
    assert "crr: 2 live" in s


def test_a_newline_in_the_reason_cannot_break_the_one_line_invariant():
    # reason is cosmetic display text for the OS's blocking UI, so a bad
    # reason must degrade the MESSAGE, never the HOLD. Confirmed live
    # 2026-08-13: before this fix, holder_script({"sleep","shutdown"},
    # "crr: a\nb") emitted a 3-top-level-line script that silently
    # executed NOTHING when piped to the real host -- alive at 12s
    # against a 2.16s deadline, exit 0, zero stderr, only EOF ended it.
    # held() would report both locks acquired while nothing was held.
    clean = holder_script(frozenset({"sleep", "shutdown"}), "crr: a live")
    dirty_lf = holder_script(frozenset({"sleep", "shutdown"}), "crr: a\nb")
    dirty_crlf = holder_script(frozenset({"sleep", "shutdown"}), "crr: a\r\nb")
    clean_lines = [line for line in clean.splitlines() if line.strip()]
    for dirty in (dirty_lf, dirty_crlf):
        dirty_lines = [line for line in dirty.splitlines() if line.strip()]
        assert len(dirty_lines) == len(clean_lines) == 2, (
            "a newline (or CRLF) in reason must not add a top-level line "
            "-- that is exactly what breaks the one-PowerShell-statement "
            "invariant and silences the whole script")
        assert "crr: a b" in dirty, "sanitized reason should stay readable"


def test_a_control_character_in_the_reason_is_stripped():
    s = holder_script(frozenset({"sleep", "shutdown"}), "crr: a\x00\x07b")
    lines = [line for line in s.splitlines() if line.strip()]
    assert len(lines) == 2
    assert "\x00" not in s and "\x07" not in s
    assert "crr: a b" in s


def test_a_quote_in_the_reason_is_still_escaped():
    s = holder_script(frozenset({"sleep", "shutdown"}), "crr: it's live")
    assert "it''s live" in s


def test_script_exits_when_stdin_closes():
    # THE orphan defence. Without this a killed crr leaves a PowerShell
    # holding a shutdown block forever, and the user has a machine that
    # refuses to restart with nothing left running to explain why.
    # NOT [Console]::In / ReadLine: that reader blocks the calling thread
    # synchronously (measured live, see the module docstring), so the
    # mechanism reads the raw stdin stream instead.
    s = holder_script(frozenset({"sleep"}), "r")
    assert "OpenStandardInput" in s and "ReadAsync" in s, (
        "no async stdin read: an orphan would hold forever")


def test_script_self_releases_after_the_cap():
    s = holder_script(frozenset({"sleep"}), "r", max_hours=12)
    assert "12" in s


def test_the_deadline_is_a_bounded_wait_not_a_loop_around_a_blocking_read():
    # [Console]::In.ReadLine() blocks synchronously. A `while (deadline) {
    # ReadLine() }` loop only re-checks the deadline BETWEEN completed
    # reads -- if hold() never writes a second line, the process enters
    # ReadLine once and blocks there for the rest of its life, and the
    # deadline never fires. Confirmed live 2026-08-12: a real holder with
    # max_hours=0.0006 (~2.16s) and stdin left open (no further writes,
    # no EOF) was still alive at t=25s.
    #
    # The first fix attempt swapped in [Console]::In.ReadLineAsync()
    # awaited via .Wait(ms) -- ALSO wrong, caught the same way. Confirmed
    # live 2026-08-13: the assignment `$readTask =
    # [Console]::In.ReadLineAsync()` itself did not return for 30+
    # seconds with stdin open and no data sent, because [Console]::In
    # wraps a SyncTextReader whose "async" methods still run
    # synchronously on the calling thread. The working fix reads the RAW
    # stream from [Console]::OpenStandardInput() (genuinely async) and
    # bounds it with Task.WaitAny -- confirmed live on both branches:
    # timeout fired at ~2.24s for a 2159ms deadline with stdin held open,
    # and a real EOF completed the read at ~1.97s against a 30s deadline.
    s = holder_script(frozenset({"sleep"}), "r", max_hours=1)
    assert "while" not in s, (
        "a while loop wrapped around a blocking ReadLine is exactly the "
        "shape that let the deadline go dead: it only re-checks between "
        "completed reads, and hold() never sends a second line")
    assert "[Console]::In" not in s, (
        "[Console]::In wraps a SyncTextReader: ReadLineAsync() on it "
        "blocks the calling thread until data/EOF, so .Wait(ms) never "
        "gets to time out. Measured 2026-08-13: 30s+ with stdin open. "
        "Use [Console]::OpenStandardInput() -- the unwrapped Stream.")
    assert "OpenStandardInput" in s and "ReadAsync" in s
    assert re.search(r"WaitAny\(\s*@\(\$readTask\)\s*,\s*\[int\]\d+\s*\)", s), (
        "expected a single bounded wait carrying a precomputed "
        "millisecond literal, e.g. "
        "[System.Threading.Tasks.Task]::WaitAny(@($readTask), [int]3600000)")
    expected_ms = 1 * 3600 * 1000
    assert str(expected_ms) in s


def test_the_deadline_wait_clamps_to_int32_max_ms():
    # A caller passing an absurd max_hours must not overflow into a
    # negative wait, which Task.WaitAny(int) would either reject or treat
    # as "return immediately" -- silently defeating the backstop.
    s = holder_script(frozenset({"sleep"}), "r", max_hours=10**9)
    match = re.search(r"WaitAny\(\s*@\(\$readTask\)\s*,\s*\[int\](\d+)\s*\)", s)
    assert match is not None
    assert int(match.group(1)) <= 2147483647


def test_the_pinvoke_signature_is_not_a_herestring():
    # @" ... "@ here-strings, found while re-verifying the WaitAny fix
    # above against the real host. Confirmed live 2026-08-13: fed through
    # powershell.exe -NoProfile -Command - over a piped (not console)
    # stdin, a @" ... "@ block silently executes NOTHING in the script
    # that contains it -- not the assignment, not any statement before or
    # after it -- exit code 0, zero output, every time, regardless of the
    # here-string's content (reproduced with a trivial one-line body, not
    # just the real DllImport signatures). The fix keeps the signature
    # block a single PowerShell source line: a normal double-quoted
    # string with `n-escaped line breaks and backtick-escaped inner
    # quotes, so the STRING VALUE is multi-line C# without the PowerShell
    # SOURCE ever spanning multiple lines.
    s = holder_script(frozenset({"sleep", "shutdown"}), "r")
    assert '@"' not in s, (
        "a here-string block silently swallows the whole script when "
        "piped to powershell.exe -Command - over non-console stdin -- "
        "confirmed live 2026-08-13, exit 0 with zero output every time")
    assert "SetThreadExecutionState" in s
    assert "ShutdownBlockReasonCreate" in s


def test_the_script_exits_explicitly_rather_than_falling_off_the_end():
    # powershell.exe -Command - behaves like an interactive session: once
    # it finishes running whatever was piped to it, it goes back to
    # reading stdin for the NEXT command -- it does not quit on its own.
    # Confirmed live 2026-08-13 with full tracing: every statement ran to
    # completion (including the WaitAny deadline firing and both release
    # calls) in ~2.4s, and the *process* was still alive and reported so
    # 30 seconds later. Releasing the locks is necessary but not
    # sufficient for "the orphan is gone" -- the process itself has to
    # exit too. [Environment]::Exit(0), not a bare `exit`: see the next
    # test for why the exit call has to be the last thing inside a
    # single wrapped statement rather than its own top-level line.
    s = holder_script(frozenset({"sleep"}), "r")
    assert "[Environment]::Exit(0)" in s, (
        "the script must end by explicitly exiting the PowerShell host "
        "process -- falling off the end leaves it alive, waiting for "
        "the next interactive command on the same still-open stdin")


def test_the_whole_script_is_one_statement_so_nothing_is_left_to_steal():
    # The deepest bug found while re-verifying the WaitAny fix: reading
    # raw stdin FROM WITHIN a script that -Command - is ITSELF still
    # reading the rest of from the SAME kernel pipe races the two
    # readers. Confirmed live 2026-08-13, 100% reproducible for a fixed
    # script: whenever PowerShell statements remained unparsed after the
    # WaitAny read (true for sleep+shutdown, which has release calls
    # after the wait), the internal 1-byte $stdin.ReadAsync sometimes won
    # the race and stole a byte -Command - had not yet consumed as "the
    # rest of the script", corrupting the trailing text by exactly one
    # dropped character (SetThreadExecutionState -> SetThreadExectionState;
    # uint32 -> unt32) and making WaitAny complete almost instantly
    # instead of honouring the deadline. Not an orphaned process -- the
    # stolen byte still made WaitAny return, so the release calls and
    # [Environment]::Exit(0) still ran, just ~1.7 hours early for a
    # 2-hour test. held() would silently report a hold that had already
    # stopped holding anything. Wrapping the ENTIRE body as a single
    # `& { stmt1; stmt2; ...; }` statement forces the parser to consume
    # it all before executing any of it, so there is nothing of our own
    # script left in the pipe for the internal read to steal from.
    s = holder_script(frozenset({"sleep", "shutdown"}), "r")
    lines = [line for line in s.splitlines() if line.strip()]
    assert len(lines) == 2, (
        "expected exactly two top-level lines: a leading `# max_hours=` "
        "comment (which cannot live inside the statement below -- a `#` "
        "comment eats the rest of ITS OWN line) and the single wrapped "
        f"statement carrying everything else. Got {len(lines)}: {lines!r}")
    comment_line, stmt_line = lines
    assert comment_line.startswith("#")
    assert stmt_line.startswith("& {") and stmt_line.endswith("}"), (
        "the script body must be a single wrapped statement -- trailing "
        "top-level lines after the stdin read are exactly what the "
        "byte-stealing race corrupts")


def test_argv_runs_powershell_noninteractively_with_stdin_open():
    argv = holder_argv()
    assert argv[0] == "powershell.exe"
    assert "-NoProfile" in argv
    assert "-Command" in argv
    assert "-NonInteractive" not in argv, (
        "the holder READS stdin as its liveness signal; -NonInteractive "
        "would defeat the orphan defence")


def test_a_stdin_eof_child_exits_promptly():
    # Platform-independent proof of the MECHANISM the Windows script uses:
    # a child reading stdin to EOF must exit when the pipe closes. The
    # PowerShell equivalent is asserted by inspection above; this asserts
    # the pattern actually terminates a process.
    proc = _sp.Popen([_sys.executable, "-c",
                      "import sys; sys.stdin.read()"], stdin=_sp.PIPE)
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0
