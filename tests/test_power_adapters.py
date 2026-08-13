"""Power adapters (spec 2026-08-12).

The AC probe is measured, not assumed: WSL2 passes the host battery
through sysfs (`/sys/class/power_supply/AC1/online`), which is why ONE
Linux adapter serves both native Linux and WSL.
"""

import os
import re
import subprocess as _sp
import sys as _sys
import time
from pathlib import Path

import pytest

from crr.adapters.power_source import (MacPowerSource, SysfsPowerSource,
                                       _parse_pmset)
from crr.adapters.power_hold_linux import (LinuxPowerHolder, inhibit_argv,
                                           lid_exemption, lid_is_exempt,
                                           logind_sources)
from crr.adapters.power_hold_macos import MacPowerHolder, caffeinate_argv
from crr.adapters.power_hold_windows import (WindowsPowerHolder,
                                             holder_argv, holder_script)

from crr import cli as _cli
from crr.adapters.power_hold_linux import LinuxPowerHolder as _L
from crr.adapters.power_hold_macos import MacPowerHolder as _M
from crr.adapters.power_hold_windows import WindowsPowerHolder as _W


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


def _logind(root: Path, rel: str, text: str) -> Path:
    """Write a logind config source at ``rel`` under a fake filesystem root.

    ``rel`` is always relative -- `root / "/etc/..."` would silently
    discard `root` and point at the REAL host config.
    """
    assert not rel.startswith("/"), "rel must be relative or root is discarded"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _LiveProc:
    """A spawn result that stays alive and reaps cleanly."""

    def __init__(self): self.terminated = False
    def poll(self): return None
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): return 0


def test_holder_refuses_to_block_sleep_when_the_lid_is_not_exempt(tmp_path):
    # The builder's hard requirement is that closing the lid always
    # sleeps. On a host that has turned the default off, a sleep lock
    # would break that, so crr withholds instead.
    _logind(tmp_path, "etc/systemd/logind.conf",
            "LidSwitchIgnoreInhibited=no\n")
    spawned = []
    holder = LinuxPowerHolder(conf_root=tmp_path,
                              spawn=lambda argv, **kw: spawned.append(argv))
    holder.hold(frozenset({"sleep"}), "r")
    assert spawned == [], "blocked sleep on a host where that blocks the lid"
    assert holder.held() == frozenset()


def test_holder_still_blocks_shutdown_when_the_lid_is_not_exempt(tmp_path):
    # Only the sleep half is unsafe there; shutdown is unaffected by lid
    # handling, so withholding it too would be over-correction.
    _logind(tmp_path, "etc/systemd/logind.conf",
            "LidSwitchIgnoreInhibited=no\n")
    spawned = []

    def _spawn(argv, **kw):
        spawned.append(argv)
        return _LiveProc()

    holder = LinuxPowerHolder(conf_root=tmp_path, spawn=_spawn)
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"shutdown"})
    what = spawned[0][spawned[0].index("--what") + 1]
    assert what == "shutdown"


def test_capabilities_are_both_on_linux(tmp_path):
    holder = LinuxPowerHolder(conf_root=tmp_path / "absent")
    assert holder.capabilities() == frozenset({"sleep", "shutdown"})


# --- the effective logind config, not just one file -----------------------
# logind's RECOMMENDED override mechanism is a drop-in, not an edit to
# logind.conf. A holder that reads only /etc/systemd/logind.conf therefore
# reads a file the host may have deliberately overridden -- and the failure
# is the one thing that must never happen: `LidSwitchIgnoreInhibited=no` in
# a drop-in, read as the compiled-in `yes`, so crr holds `sleep` and closing
# the lid stops suspending the machine.

@pytest.mark.parametrize("rel", [
    "etc/systemd/logind.conf.d/90-crr.conf",
    "run/systemd/logind.conf.d/90-crr.conf",
    "usr/lib/systemd/logind.conf.d/90-crr.conf",
])
def test_a_dropin_saying_no_withholds_sleep_from_every_dropin_dir(tmp_path, rel):
    # A stock main file that never mentions the key, plus a drop-in that
    # turns the exemption off -- exactly the shape a distro package ships.
    _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n#NAutoVTs=6\n")
    _logind(tmp_path, rel, "[Login]\nLidSwitchIgnoreInhibited=no\n")
    assert lid_exemption(tmp_path) is False, (
        f"{rel} overrides the main file; missing it means crr holds sleep "
        "and closing the lid no longer suspends")


def test_the_usr_lib_main_conf_is_a_source_too(tmp_path):
    # On Fedora-likes /usr/lib/systemd/logind.conf is the ONLY main file;
    # /etc/systemd/logind.conf does not exist at all.
    _logind(tmp_path, "usr/lib/systemd/logind.conf",
            "[Login]\nLidSwitchIgnoreInhibited=no\n")
    assert lid_exemption(tmp_path) is False


def test_a_dropin_that_says_nothing_leaves_the_default_alone(tmp_path):
    # The real drop-in on this host (unattended-upgrades) sets an unrelated
    # key. Treating any drop-in's mere existence as "not exempt" would make
    # crr refuse to block sleep on every Ubuntu box -- protecting nothing,
    # from the other direction.
    _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    _logind(tmp_path, "usr/lib/systemd/logind.conf.d/10-maxdelay.conf",
            "[Login]\nInhibitDelayMaxSec=30\n")
    assert lid_exemption(tmp_path) is True


def test_no_config_source_at_all_is_the_compiled_in_default_not_unknown(tmp_path):
    # KNOWN, not unknown: with no config anywhere, logind uses its
    # compiled-in LidSwitchIgnoreInhibited=yes. Mirrors the AC probe's
    # empty-directory-is-a-desktop case.
    assert lid_exemption(tmp_path) is True


def test_a_source_that_exists_but_cannot_be_read_is_unknown_not_safe(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    conf = _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    conf.chmod(0o000)
    try:
        assert lid_exemption(tmp_path) is None, (
            "never read the config is not the same as safe to hold sleep")
    finally:
        conf.chmod(0o644)


def test_a_dropin_dir_that_cannot_be_listed_is_unknown_not_empty(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    d = tmp_path / "etc/systemd/logind.conf.d"
    d.mkdir(parents=True)
    d.chmod(0o000)
    try:
        assert lid_exemption(tmp_path) is None, (
            "an unlistable drop-in dir is unknown; reporting it as empty is "
            "the same defect as reporting an unreadable file as exempt")
    finally:
        d.chmod(0o755)


def test_a_definite_no_beats_an_unreadable_sibling(tmp_path):
    # Precedence-proof by construction: ANY source saying no wins, so the
    # answer never depends on implementing logind's precedence rules.
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    _logind(tmp_path, "etc/systemd/logind.conf",
            "LidSwitchIgnoreInhibited=no\n")
    other = _logind(tmp_path, "usr/lib/systemd/logind.conf", "[Login]\n")
    other.chmod(0o000)
    try:
        assert lid_exemption(tmp_path) is False
    finally:
        other.chmod(0o644)


def test_the_collector_finds_every_source_logind_would_read(tmp_path):
    main = _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    lib_main = _logind(tmp_path, "usr/lib/systemd/logind.conf", "[Login]\n")
    dropin = _logind(tmp_path, "run/systemd/logind.conf.d/50-x.conf", "[Login]\n")
    _logind(tmp_path, "run/systemd/logind.conf.d/notes.txt", "ignored\n")
    paths, complete = logind_sources(tmp_path)
    assert complete is True
    found = set(paths)
    assert {main, lib_main, dropin} <= found
    assert not any(p.name.endswith(".txt") for p in paths), (
        "logind reads *.conf drop-ins only")


def test_the_real_host_config_is_exempt_so_crr_is_not_over_corrected(tmp_path):
    # Guard against the opposite failure: an implementation that returns
    # False/None on a stock box never blocks sleep anywhere. Measured
    # 2026-08-13 on this host -- a readable /etc/systemd/logind.conf plus
    # /usr/lib/systemd/logind.conf.d/unattended-upgrades-logind-maxdelay.conf,
    # neither setting the key.
    if not Path("/etc/systemd/logind.conf").exists():
        pytest.skip("no logind config on this host")
    if lid_exemption(Path("/")) is None:
        pytest.skip("host logind config is unreadable by this user")
    assert lid_exemption(Path("/")) is True


def test_the_withheld_reason_does_not_claim_a_setting_it_never_read(tmp_path):
    # Two different withholdings with two different reasons. Reporting
    # "this host sets LidSwitchIgnoreInhibited=no" when the config could
    # not be read is a confident claim about a fact never established.
    if os.geteuid() == 0:
        pytest.skip("running as root; chmod 000 is ignored")
    conf = _logind(tmp_path, "etc/systemd/logind.conf", "[Login]\n")
    conf.chmod(0o000)
    try:
        holder = LinuxPowerHolder(conf_root=tmp_path,
                                  spawn=lambda argv, **kw: _LiveProc())
        holder.hold(frozenset({"sleep"}), "r")
    finally:
        conf.chmod(0o644)
    assert holder.held() == frozenset()
    reason = holder.withheld() or ""
    assert "LidSwitchIgnoreInhibited=no" not in reason, (
        f"claims a setting it never read: {reason!r}")
    assert "read" in reason or "unknown" in reason, reason


# --- the spawn either worked or it did not --------------------------------

def test_a_systemd_inhibit_that_fails_is_not_reported_as_a_hold(tmp_path):
    # Measured on this host (WSL, no logind session), 2026-08-13:
    #   systemd-inhibit --what=sleep --mode=block --who=crr --why=x sleep 1
    #   -> stderr "Failed to inhibit: Access denied", exit 1, in
    #   milliseconds.
    # With stderr=DEVNULL and an unconditional `self._held = effective`,
    # held() reported the full set, then reported empty with withheld()
    # None -- no reason recorded anywhere, because the stderr that
    # explained it was discarded at the source.
    fail = [_sys.executable, "-c",
            "import sys; sys.stderr.write('Failed to inhibit: Access denied\\n');"
            " sys.exit(1)"]

    def _spawn(argv, **kw):
        return _sp.Popen(fail, **kw)   # a REAL process, real exit, real stderr

    holder = LinuxPowerHolder(conf_root=tmp_path, spawn=_spawn)
    holder.hold(frozenset({"sleep"}), "r")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and holder.held():
        time.sleep(0.02)
    assert holder.held() == frozenset(), (
        "reported a hold from a systemd-inhibit that had already exited 1")
    reason = holder.withheld() or ""
    assert "Access denied" in reason, (
        f"the stderr that explains the failure must survive: {reason!r}")
    holder.release()


def test_release_never_reads_stderr_from_a_child_it_could_not_reap(tmp_path):
    # `stream.read()` on a LIVE child's pipe does not raise -- it blocks
    # until EOF, i.e. forever, wedging the poll loop this adapter exists to
    # stay off. So the drain must be gated on a CONFIRMED exit, not run
    # unconditionally after a wait() that may have timed out.
    # A raising stub would NOT discriminate: _drain_stderr catches
    # Exception, so the raise is swallowed and the broken version passes.
    # The real failure is a BLOCK, so record the call instead.
    class _Stderr:
        def __init__(self): self.read_called = False

        def read(self):
            self.read_called = True      # in reality: blocks until EOF
            return b""

        def close(self): pass

    class _Unreapable:
        def __init__(self): self.stderr = _Stderr()
        def poll(self): return None
        def terminate(self): pass

        def wait(self, timeout=None):
            raise _sp.TimeoutExpired(cmd="systemd-inhibit", timeout=timeout)

    proc = _Unreapable()
    holder = LinuxPowerHolder(conf_root=tmp_path,
                              spawn=lambda argv, **kw: proc)
    holder.hold(frozenset({"sleep"}), "r")
    holder.release()
    assert not proc.stderr.read_called, (
        "drained stderr from a child that wait() never confirmed dead -- "
        "that read blocks until EOF, wedging the poll loop forever")
    # NOT frozenset() -- issue #77. wait() never confirmed this child
    # dead, so it may still hold systemd-inhibit's lock; release() must
    # keep reporting the set rather than clearing it unconditionally.
    assert holder.held() == frozenset({"sleep"})


def test_a_live_inhibit_still_reports_its_hold(tmp_path):
    # The reap must not turn a WORKING hold into a withheld one.
    stay = [_sys.executable, "-c", "import time; time.sleep(30)"]
    holder = LinuxPowerHolder(conf_root=tmp_path,
                              spawn=lambda argv, **kw: _sp.Popen(stay, **kw))
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    try:
        assert holder.held() == frozenset({"sleep", "shutdown"})
        assert holder.withheld() is None
    finally:
        holder.release()
    assert holder.held() == frozenset()


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
    # `"12" in s` was the original assertion and it proved nothing: the
    # emitted script carries a leading `# max_hours=12` comment, so a
    # completely dead deadline still passed. Assert the millisecond value
    # actually inside the bounded wait.
    s = holder_script(frozenset({"sleep"}), "r", max_hours=12)
    match = re.search(r"WaitAny\(\s*@\(\$readTask\)\s*,\s*\[int\](\d+)\s*\)", s)
    assert match is not None, "no bounded wait carrying a deadline at all"
    assert int(match.group(1)) == 12 * 3600 * 1000, (
        "the cap must reach the wait, not just the comment above it")


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


# --- release must never lose the handle to a live shutdown blocker --------

class _StubStdin:
    def __init__(self, raise_on_write=None):
        self.closed = False
        self.written = []
        self._raise = raise_on_write

    def write(self, text):
        if self._raise is not None:
            raise self._raise
        self.written.append(text)

    def flush(self): pass
    def close(self): self.closed = True


class _StubProc:
    """A PowerShell stand-in whose first N waits time out."""

    def __init__(self, waits_that_timeout=0, dies_on="", stdin=None):
        self.stdin = _StubStdin() if stdin is None else stdin
        self._left = waits_that_timeout
        self._dies_on = dies_on        # "" | "terminate" | "kill" | "never"
        self.calls = []
        self.alive = True

    def poll(self): return None if self.alive else 0

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self._left > 0:
            self._left -= 1
            raise _sp.TimeoutExpired(cmd="powershell.exe", timeout=timeout)
        self.alive = False
        return 0

    def terminate(self):
        self.calls.append("terminate")
        if self._dies_on == "terminate":
            self._left = 0

    def kill(self):
        self.calls.append("kill")
        if self._dies_on == "kill":
            self._left = 0


def _windows_holder_holding(proc):
    holder = WindowsPowerHolder(spawn=lambda argv, **kw: proc)
    holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset({"sleep", "shutdown"})
    return holder


def test_release_escalates_and_confirms_the_reap_before_forgetting_it():
    # If wait(timeout=10) raises, terminate() alone is not proof of death.
    # Clearing _proc/_held there leaves crr with NO handle to a PowerShell
    # that may still hold ShutdownBlockReasonCreate: permanently
    # uncleanable, held() says nothing is held, and the user gets a machine
    # that refuses to restart with nothing left to explain why.
    proc = _StubProc(waits_that_timeout=1, dies_on="terminate")
    holder = _windows_holder_holding(proc)
    holder.release()
    assert proc.stdin.closed, "the EOF signal must still be sent first"
    assert proc.calls.count("wait") >= 2, (
        f"terminate() with no re-wait is not a confirmed reap: {proc.calls}")
    assert "terminate" in proc.calls
    assert holder.held() == frozenset()


def test_release_kills_when_terminate_is_not_enough():
    proc = _StubProc(waits_that_timeout=2, dies_on="kill")
    holder = _windows_holder_holding(proc)
    holder.release()
    assert proc.calls.count("terminate") == 1
    assert proc.calls.count("kill") == 1
    assert proc.calls.count("wait") >= 3
    assert holder.held() == frozenset()


def test_release_keeps_reporting_a_process_it_could_not_reap():
    # Measured normal teardown is ~2.07s, so exhausting terminate AND kill
    # means something is genuinely wrong. Silently reporting "nothing is
    # held" there is the branch's whole defect class: succeeding loudly
    # while a process may still be blocking restart. Keep the handle so the
    # next hold() can retry, and keep saying what may still be held.
    proc = _StubProc(waits_that_timeout=99, dies_on="never")
    holder = _windows_holder_holding(proc)
    holder.release()
    assert "kill" in proc.calls
    assert holder.held() == frozenset({"sleep", "shutdown"}), (
        "an unreaped PowerShell may still hold the shutdown block; saying "
        "nothing is held is the lie that makes it uncleanable")
    proc._left = 0                       # it finally dies
    holder.release()
    assert holder.held() == frozenset()


def test_a_broken_pipe_on_the_script_write_still_leaves_a_tracked_child():
    # _proc assigned AFTER the write means a BrokenPipeError orphans a
    # child crr has no handle to -- on Windows that child does not die with
    # its WSL parent.
    proc = _StubProc(stdin=_StubStdin(raise_on_write=BrokenPipeError()))
    holder = WindowsPowerHolder(spawn=lambda argv, **kw: proc)
    with pytest.raises(BrokenPipeError):
        holder.hold(frozenset({"sleep", "shutdown"}), "r")
    assert holder.held() == frozenset(), (
        "a script that never reached the child holds nothing")
    holder.release()
    assert proc.calls, f"release() could not reach the child: {proc.calls}"
    assert not proc.alive


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


def test_wsl_selects_the_windows_holder_despite_reporting_linux():
    # platform.system() is "Linux" on WSL, so the obvious detect()-shaped
    # selection picks systemd-inhibit — which runs INSIDE the VM and
    # cannot affect the Windows host's power state at all. It would hold
    # successfully and protect nothing.
    assert isinstance(_cli._power_holder("Linux", wsl=True), _W)


def test_native_linux_selects_systemd_inhibit():
    assert isinstance(_cli._power_holder("Linux", wsl=False), _L)


def test_macos_selects_caffeinate():
    assert isinstance(_cli._power_holder("Darwin", wsl=False), _M)


def test_an_unsupported_platform_raises_rather_than_pretending():
    import pytest
    with pytest.raises(NotImplementedError) as e:
        _cli._power_holder("Plan9", wsl=False)
    assert "Plan9" in str(e.value)


def test_power_source_uses_sysfs_on_wsl_because_the_host_battery_is_exposed():
    from crr.adapters.power_source import SysfsPowerSource
    assert isinstance(_cli._power_source("Linux", 5.0), SysfsPowerSource)


# --- Linux release must never lose the handle to a live inhibitor ---------
# Issue #77: LinuxPowerHolder.release() called terminate() then wait(5) and
# dropped _proc/_held UNCONDITIONALLY, with no kill() escalation and no
# check of whether the wait actually confirmed death. A child that ignores
# SIGTERM leaves crr with no handle to a process that may still hold
# systemd-inhibit's lock, while held() reports nothing is held -- the SAME
# defect fixed on the Windows side (see the _StubProc release tests above).
# Both platforms now share the ladder in crr/adapters/_proc.py.

class _LinuxStubProc:
    """A systemd-inhibit stand-in whose first N waits time out."""

    def __init__(self, waits_that_timeout=0, dies_on=""):
        self._left = waits_that_timeout
        self._dies_on = dies_on        # "" | "terminate" | "kill" | "never"
        self.calls = []
        self.alive = True
        self.stderr = None

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self._left > 0:
            self._left -= 1
            raise _sp.TimeoutExpired(cmd="systemd-inhibit", timeout=timeout)
        self.alive = False
        return 0

    def terminate(self):
        self.calls.append("terminate")
        if self._dies_on == "terminate":
            self._left = 0

    def kill(self):
        self.calls.append("kill")
        if self._dies_on == "kill":
            self._left = 0


def _linux_holder_holding(proc, tmp_path):
    holder = LinuxPowerHolder(conf_root=tmp_path, spawn=lambda argv, **kw: proc)
    holder.hold(frozenset({"sleep"}), "r")
    assert holder.held() == frozenset({"sleep"})
    return holder


def test_linux_release_keeps_the_handle_when_the_child_cannot_be_reaped(tmp_path):
    # THE red test for issue #77. Against today's (buggy) release(), this
    # fails: the old code calls terminate() then wait(5) once, then clears
    # _proc/_held no matter what -- even a child whose wait() ever
    # confirms death. A live child may still hold systemd-inhibit's lock;
    # reporting held() empty there is the same lie the Windows holder was
    # fixed for.
    proc = _LinuxStubProc(waits_that_timeout=99, dies_on="never")
    holder = _linux_holder_holding(proc, tmp_path)
    holder.release()
    assert holder._proc is not None, (
        "release() dropped the handle to a child it never confirmed dead")
    assert holder.held() == frozenset({"sleep"}), (
        "an unreaped systemd-inhibit may still hold the lock; reporting "
        "nothing held is the lie that makes it uncleanable")


def test_linux_release_escalates_terminate_before_kill(tmp_path):
    # kill must never be tried before terminate, and must only be reached
    # once an earlier wait has already failed to confirm death.
    proc = _LinuxStubProc(waits_that_timeout=2, dies_on="kill")
    holder = _linux_holder_holding(proc, tmp_path)
    holder.release()
    assert "terminate" in proc.calls and "kill" in proc.calls
    assert proc.calls.index("terminate") < proc.calls.index("kill"), (
        f"kill before terminate: {proc.calls}")
    assert proc.calls.count("wait") >= 2, (
        f"kill must only be reached after an earlier wait failed: {proc.calls}")
    assert holder.held() == frozenset()


def test_linux_release_confirms_a_normal_reap_and_drops_the_handle(tmp_path):
    proc = _LinuxStubProc(waits_that_timeout=0)
    holder = _linux_holder_holding(proc, tmp_path)
    holder.release()
    assert holder._proc is None
    assert holder.held() == frozenset()
