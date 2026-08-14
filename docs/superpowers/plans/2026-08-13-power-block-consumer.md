# Power Blocking (the consumer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the power holders actually run — a long-lived `crr awake` loop
driven by a dedicated service unit, plus the visibility and the off switch
that make it safe to leave on.

**Architecture:** Phase 1a shipped a pure `decide()` and three
`PowerHolder` adapters that nothing calls. This plan adds the composition:
a poll step that reads the journal, asks the AC probe, calls `decide()`, and
applies the result to the holder; a `crr awake` command that runs that step
on an interval and **releases on SIGTERM**; a `crr-awake` unit (systemd and
launchd) to run it; and `crr power` / `crr doctor` so a hold is never
invisible.

**Tech Stack:** Python 3.11+ stdlib only. systemd user units, launchd user
agents. pytest, import-linter.

## Global Constraints

- **Runtime deps stay at zero.** Adding anything to `pyproject.toml`'s
  runtime deps is a design regression (AGENTS.md).
- **One-way layering, machine-enforced:** `crr.cli` → `crr.adapters` →
  `crr.core`. `crr.core` must never import adapters or cli.
- **Test-first.** Every behaviour gets a failing test before implementation.
- **Every judgment-call constant is a named config key** in
  `crr/core/config.py` `DEFAULTS` with a `CONFIG_DEFAULTS_VERSION` bump —
  never a literal in logic (`tests/test_priors.py` is the guard).
- **Lid close is never blocked, on any platform.**
- **Null results stay null.** An unknown must never become a positive claim.
- **A hold must never be invisible.** A machine that will not sleep, with no
  visible cause, is the genre of mystery this project exists to eliminate.
- Run before every commit: `.venv/bin/python -m pytest tests/ -q` and
  `.venv/bin/lint-imports`. The pre-commit hook runs both and blocks.
- `crr` on PATH is the **deployed** copy. Use `.venv/bin/crr` when testing.
- Spec: `docs/superpowers/specs/2026-08-12-power-block-design.md`.
- Phase 1a (merged, `4b4863e`): `crr/core/power.py`, `crr/core/ports.py`
  `PowerSource`/`PowerHolder`, `crr/adapters/power_source.py`,
  `crr/adapters/power_hold_{linux,macos,windows}.py`,
  `crr/adapters/_proc.py`'s `release_child`, and `cli._power_holder` /
  `cli._power_source`.

## What "release" means, and why there is no `crr power --release` that releases

The hold is a **child process of `crr awake`**. A separate `crr power`
process cannot release another process's child — there is no handle to
pass. Since every holder dies with its parent (that is the crash-safety
property phase 1a was built around), *releasing* the hold and *stopping the
unit* are the same operation.

So `crr power --release` **stops the unit** through the platform's service
manager, and says so. Anything else would be a button that appears to do
something and does not.

## File Structure

| File | Responsibility |
|---|---|
| `crr/cli.py` (modify) | `_live_claude_count`, `_power_poll_once`, `_cmd_awake`, `_cmd_power`, doctor lines, unit wiring |
| `crr/adapters/systemd.py` (modify) | `AWAKE_SERVICE_NAME`, `awake_service_unit`, enable/disable/stop commands |
| `crr/adapters/launchd.py` (modify) | `AWAKE_LABEL`/`AWAKE_PLIST`, `awake_agent_plist` |
| `tests/test_power_consumer.py` (create) | poll step + loop + command tests |
| `tests/test_systemd.py`, `tests/test_launchd.py` (modify) | unit/plist builders |

Out of scope, deliberately: the dashboard badge (touches `page.html` and
needs a `PAGE_VERSION` bump + pin — it belongs with the tray plan), the
Windows tray, and `crr harden`.

---

### Task 1: Count live sessions, and thread `max_hours` into the holder

**Files:**
- Modify: `crr/cli.py`
- Test: `tests/test_power_consumer.py`

**Interfaces:**
- Consumes: `cli._power_holder(system, wsl)` from phase 1a; `DEFAULTS["power_block_max_hours"]`
- Produces: `cli._live_claude_count(entries, owners) -> int`; `cli._power_holder(system, wsl, max_hours=None)`

The final review of phase 1a flagged that `_power_holder` passes no config,
so `power_block_max_hours` was dead. Fix it here, where the first caller
appears.

- [ ] **Step 1: Write the failing test**

Create `tests/test_power_consumer.py`:

```python
"""The power-block consumer: poll step, loop, and commands.

Phase 1a shipped adapters nothing called. These are the tests for the
wiring that finally calls them.
"""

import os

import pytest

from crr import cli
from crr.core.config import DEFAULTS


def test_live_claude_count_counts_only_sessions_with_a_live_owner():
    # A journal entry with a claude field is not proof the agent is alive;
    # only the process snapshot is. Counting entries instead of owners
    # would hold the machine awake for sessions that already died.
    entries = [{"pid": 1}, {"pid": 2}, {"pid": 3}]
    owners = {1: [11], 2: [], 3: [33]}
    assert cli._live_claude_count(entries, owners) == 2


def test_live_claude_count_is_zero_when_nothing_is_owned():
    assert cli._live_claude_count([{"pid": 1}], {1: []}) == 0


def test_live_claude_count_treats_a_missing_owner_entry_as_not_live():
    # `claude_group_pids` omits pids it could not resolve. Absent is not
    # alive — the spine rule, applied to the thing that decides whether
    # crr keeps a laptop awake.
    assert cli._live_claude_count([{"pid": 9}], {}) == 0


def test_power_holder_threads_the_configured_cap():
    holder = cli._power_holder("Windows", wsl=False, max_hours=3)
    assert holder._max_hours == 3


def test_power_holder_cap_defaults_to_the_named_config_prior():
    holder = cli._power_holder("Windows", wsl=False)
    assert holder._max_hours == DEFAULTS["power_block_max_hours"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_consumer.py -v`
Expected: FAIL with `AttributeError: module 'crr.cli' has no attribute '_live_claude_count'`

- [ ] **Step 3: Write minimal implementation**

In `crr/cli.py`, add beside `_power_holder`:

```python
def _live_claude_count(entries, owners) -> int:
    """How many journaled sessions have a LIVE claude process right now.

    Counting entries instead of owners would keep the machine awake for
    conversations that already ended — a journal row is a record, not a
    heartbeat. A pid missing from ``owners`` is not live: absent is not
    alive.
    """
    return sum(1 for e in entries if owners.get(e["pid"]))
```

Change `_power_holder`'s signature and both Windows branches:

```python
def _power_holder(system: str, wsl: bool, max_hours: float | None = None):
    ...
    cap = DEFAULTS["power_block_max_hours"] if max_hours is None else max_hours
    if wsl:
        return power_hold_windows.WindowsPowerHolder(max_hours=cap)
    if system == "Linux":
        return power_hold_linux.LinuxPowerHolder()
    if system == "Darwin":
        return power_hold_macos.MacPowerHolder()
    if system == "Windows":
        return power_hold_windows.WindowsPowerHolder(max_hours=cap)
    raise NotImplementedError(f"no power-hold adapter for {system!r} yet")
```

Ensure `DEFAULTS` is imported in `crr/cli.py` (it imports `crr.core.config`
as `cfg` — use whichever name that module already uses; do not add a second
import style).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power_consumer.py -v && .venv/bin/lint-imports`
Expected: 5 passed, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_power_consumer.py
git commit -m "feat(cli): live-session count, and the max_hours prior finally reaches the holder"
```

---

### Task 2: The poll step

**Files:**
- Modify: `crr/cli.py`
- Test: `tests/test_power_consumer.py`

**Interfaces:**
- Consumes: `power.decide`, `power.unmet`, `_live_claude_count`, `PowerSource`, `PowerHolder`
- Produces: `cli._power_poll_once(holder, source, entries, owners, config) -> power.Decision`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power_consumer.py`:

```python
class _FakeHolder:
    def __init__(self, caps=frozenset({"sleep", "shutdown"})):
        self._caps = caps
        self.calls = []
        self._held = frozenset()

    def capabilities(self):
        return self._caps

    def hold(self, want, reason):
        self.calls.append(("hold", want, reason))
        self._held = want & self._caps

    def release(self):
        self.calls.append(("release",))
        self._held = frozenset()

    def held(self):
        return self._held


class _FakeSource:
    def __init__(self, value):
        self.value = value

    def on_ac(self):
        return self.value


def _cfg(**over):
    base = {"power_block": "sleep+shutdown", "power_block_requires_ac": True}
    base.update(over)
    return base


def test_poll_holds_when_a_session_is_live_and_on_ac():
    holder, source = _FakeHolder(), _FakeSource(True)
    d = cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert d.want == frozenset({"sleep", "shutdown"})
    assert holder.calls[0][0] == "hold"
    assert "1 Claude session" in holder.calls[0][2]


def test_poll_releases_when_the_last_session_ends():
    holder, source = _FakeHolder(), _FakeSource(True)
    cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    holder.calls.clear()
    cli._power_poll_once(holder, source, [{"pid": 1}], {1: []}, _cfg())
    assert holder.calls == [("release",)]
    assert holder.held() == frozenset()


def test_poll_releases_on_battery():
    holder, source = _FakeHolder(), _FakeSource(False)
    d = cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert d.want == frozenset()
    assert d.withheld and "battery" in d.withheld
    assert holder.calls == [("release",)]


def test_poll_releases_when_the_power_source_cannot_be_read():
    holder, source = _FakeHolder(), _FakeSource(None)
    d = cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert d.want == frozenset()
    assert d.withheld and "cannot tell" in d.withheld
    assert holder.calls == [("release",)]


def test_poll_does_not_ask_the_source_when_ac_is_not_required():
    # A probe that is never consulted cannot fail, and on a desktop the
    # question is meaningless. Skipping it also keeps the poll cheap.
    class _Boom:
        def on_ac(self):
            raise AssertionError("power source consulted despite requires_ac=False")

    holder = _FakeHolder()
    d = cli._power_poll_once(holder, _Boom(), [{"pid": 1}], {1: [11]},
                             _cfg(power_block_requires_ac=False))
    assert d.want == frozenset({"sleep", "shutdown"})


def test_poll_is_idempotent_while_nothing_changes():
    holder, source = _FakeHolder(), _FakeSource(True)
    for _ in range(3):
        cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert [c[0] for c in holder.calls] == ["hold", "hold", "hold"]
    # The holder itself is responsible for making a repeat hold a no-op;
    # the poll step must not try to remember state the holder owns.


def test_poll_holds_only_what_the_platform_can_do():
    holder, source = _FakeHolder(caps=frozenset({"sleep"})), _FakeSource(True)
    cli._power_poll_once(holder, source, [{"pid": 1}], {1: [11]}, _cfg())
    assert holder.held() == frozenset({"sleep"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_consumer.py -k poll -v`
Expected: FAIL with `AttributeError: module 'crr.cli' has no attribute '_power_poll_once'`

- [ ] **Step 3: Write minimal implementation**

```python
def _power_poll_once(holder, source, entries, owners, config) -> power.Decision:
    """One decide-and-apply step. Returns the Decision so callers can report it.

    The AC probe is consulted ONLY when the answer can change the outcome
    — a probe that is never called cannot fail, and on a desktop the
    question is meaningless.

    Nothing here remembers what is held: the holder owns that, and a second
    copy of the state would be a second thing to get wrong.
    """
    live = _live_claude_count(entries, owners)
    requires_ac = bool(config.get("power_block_requires_ac"))
    on_ac = source.on_ac() if requires_ac else True
    decision = power.decide(
        live_sessions=live,
        on_ac=on_ac,
        mode=str(config.get("power_block")),
        requires_ac=requires_ac,
    )
    if decision.want:
        holder.hold(decision.want, decision.reason)
    else:
        holder.release()
    return decision
```

Add `from crr.core import power` to `crr/cli.py`'s core imports if absent.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_power_consumer.py -v && .venv/bin/lint-imports`
Expected: 12 passed, contract kept

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_power_consumer.py
git commit -m "feat(cli): the power poll step — decide, then apply"
```

---

### Task 3: `crr awake` — the loop that releases on the way out

**Files:**
- Modify: `crr/cli.py`
- Test: `tests/test_power_consumer.py`

**Interfaces:**
- Consumes: `_power_poll_once`, `_power_holder`, `_power_source`
- Produces: `cli._cmd_awake(args) -> int`, wired to a `crr awake` subparser with `--once`

**THE LOAD-BEARING BEHAVIOUR:** the loop must release the hold when it is
asked to stop. `systemctl --user stop crr-awake` sends SIGTERM. Without a
handler, Python dies without unwinding and on Windows the interop child is
left to its stdin-EOF fallback — which works, but relying on the fallback
when the clean path is available is how the fallback stops being tested.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power_consumer.py`:

```python
def test_awake_once_polls_exactly_once_and_exits_zero(tmp_path, monkeypatch, capsys):
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 30, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    assert cli.main(["awake", "--once"]) == 0
    assert [c[0] for c in holder.calls] == ["hold"]


def test_awake_releases_when_the_loop_is_asked_to_stop(tmp_path, monkeypatch):
    # systemctl stop sends SIGTERM. The hold must not depend on the
    # holder's own stdin-EOF fallback for the ordinary stop path.
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 0, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))

    ticks = {"n": 0}

    def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise KeyboardInterrupt   # stands in for the stop signal

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    assert cli.main(["awake"]) == 0
    assert holder.calls[-1] == ("release",), holder.calls


def test_awake_releases_even_when_a_poll_raises(tmp_path, monkeypatch, capsys):
    # A transient probe failure must not leave the machine pinned awake
    # with no loop left to release it.
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 0, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})

    def boom(*a, **k):
        raise RuntimeError("journal unreadable")

    monkeypatch.setattr(cli, "_power_entries_and_owners", boom)
    rc = cli.main(["awake", "--once"])
    assert rc != 0
    assert holder.calls[-1] == ("release",)
    assert "journal unreadable" in capsys.readouterr().err


def test_awake_rereads_config_each_poll_so_turning_it_off_takes_effect(tmp_path, monkeypatch):
    # Without this you must restart the unit to change the setting, which
    # makes the off switch feel broken.
    holder = _FakeHolder()
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    modes = iter(["sleep", "off"])
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": next(modes), "power_block_requires_ac": True,
                                 "power_poll_seconds": 0, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    ticks = {"n": 0}

    def fake_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    cli.main(["awake"])
    kinds = [c[0] for c in holder.calls]
    assert kinds[0] == "hold" and "release" in kinds[1:]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_consumer.py -k awake -v`
Expected: FAIL — `argument command: invalid choice: 'awake'`

- [ ] **Step 3: Write minimal implementation**

Add the helper the tests stub, then the command:

```python
def _power_entries_and_owners(store, probe):
    """Journaled claude sessions and their live owner pids (one snapshot)."""
    entries = [e for e in store.scan().entries if e.get("claude") is not None]
    owners = probe.claude_group_pids([e["pid"] for e in entries])
    return entries, owners


def _cmd_awake(args: argparse.Namespace) -> int:
    """[service] Hold the machine awake while a Claude session is live.

    Runs until stopped. The hold is a child of THIS process, so stopping
    this loop is what releases it — there is no other handle. The
    `finally` is therefore load-bearing, not tidiness.
    """
    system = platform.system()
    wsl = host.is_wsl()
    config = _load_config()
    try:
        holder = _power_holder(system, wsl, config.get("power_block_max_hours"))
    except NotImplementedError as exc:
        print(f"crr awake: {exc}", file=sys.stderr)
        return 2
    source = _power_source(system, config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    rc = 0
    try:
        while True:
            config = _load_config()   # re-read: turning it off must not need a restart
            probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
            entries, owners = _power_entries_and_owners(store, probe)
            _power_poll_once(holder, source, entries, owners, config)
            if args.once:
                break
            time.sleep(config.get("power_poll_seconds"))
    except KeyboardInterrupt:
        pass
    except Exception as exc:            # noqa: BLE001 - see finally
        print(f"crr awake: {exc}", file=sys.stderr)
        rc = 1
    finally:
        # LOAD-BEARING. Whatever ends this loop -- stop signal, crash, a
        # bad poll -- the hold must not outlive it. On Windows the
        # holder's stdin-EOF fallback would also catch this, but relying
        # on a fallback for the ordinary path is how the fallback stops
        # being exercised.
        holder.release()
    return rc
```

Register the subcommand beside the others in the parser builder:

```python
awake = sub.add_parser(
    "awake",
    help="[service] hold the machine awake while a Claude session is live",
)
awake.add_argument("--once", action="store_true",
                   help="run a single poll and exit (for testing and cron-style use)")
awake.set_defaults(func=_cmd_awake)
```

Ensure `time` is imported in `crr/cli.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass, contract kept

- [ ] **Step 5: Verify against this real host**

Run: `.venv/bin/crr awake --once; echo "rc=$?"`

Expected: `rc=0`, and no output (this host has `power_block="off"`, so the
poll withholds and releases). Then confirm nothing was left behind:

```bash
powershell.exe -NoProfile -Command '(Get-Process powershell -ErrorAction SilentlyContinue | Measure-Object).Count'
```

Expected: `1` (the counting process itself). If it is 2, a holder leaked
from a poll — STOP and report.

- [ ] **Step 6: Commit**

```bash
git add crr/cli.py tests/test_power_consumer.py
git commit -m "feat(cli): crr awake, and the release that must survive any exit"
```

---

### Task 4: The systemd unit

**Files:**
- Modify: `crr/adapters/systemd.py`, `crr/cli.py`
- Test: `tests/test_systemd.py`

**Interfaces:**
- Consumes: `resolve_service_path`, `_stamp`
- Produces: `systemd.AWAKE_SERVICE_NAME`, `systemd.awake_service_unit(crr_bin, path, state_home, *, restart_seconds=...)`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_systemd.py`:

```python
def test_awake_unit_is_a_long_running_service_that_restarts():
    unit = systemd.awake_service_unit("/opt/crr/bin/crr", "/usr/bin", "/home/u/.local/state")
    assert "Type=simple" in unit
    assert "ExecStart=/opt/crr/bin/crr awake" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_awake_unit_bakes_the_state_dir_like_the_other_units():
    unit = systemd.awake_service_unit("/opt/crr/bin/crr", "/usr/bin", "/home/u/.local/state")
    assert "Environment=XDG_STATE_HOME=/home/u/.local/state" in unit
    assert "Environment=PATH=/usr/bin" in unit


def test_awake_unit_stops_with_a_signal_the_loop_can_catch():
    # The loop releases the hold in a finally block on SIGTERM. A unit
    # that killed with SIGKILL would skip that, leaving the release to the
    # holder's fallback.
    unit = systemd.awake_service_unit("/opt/crr/bin/crr", "/usr/bin", "/s")
    assert "KillSignal=SIGKILL" not in unit


def test_awake_is_enabled_and_disabled_with_the_rest():
    assert any(systemd.AWAKE_SERVICE_NAME in c for c in
               [" ".join(x) for x in systemd.enable_commands()])
    assert any(systemd.AWAKE_SERVICE_NAME in c for c in
               [" ".join(x) for x in systemd.disable_commands()])


def test_awake_can_be_stopped_on_its_own():
    assert systemd.stop_awake_command() == [
        "systemctl", "--user", "stop", systemd.AWAKE_SERVICE_NAME]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_systemd.py -k awake -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'awake_service_unit'`

- [ ] **Step 3: Write minimal implementation**

In `crr/adapters/systemd.py`, beside the other names:

```python
AWAKE_SERVICE_NAME = "crr-awake.service"
```

```python
def awake_service_unit(
    crr_bin: str, path: str, state_home: str,
    *, restart_seconds: int = DEFAULTS["web_restart_seconds"],
) -> str:
    """The loop that holds the machine awake while a session is live.

    Its own unit rather than a job inside crr-web: the hold is a child of
    whatever process owns it, and tying that to the dashboard would couple
    "am I serving a page" to "may this machine sleep".

    No ``KillSignal=`` override — the default SIGTERM is what lets the loop
    release the hold in its ``finally`` before exiting.
    """
    return (
        _stamp()
        + "[Unit]\n"
        "Description=Claude-Remote-Rescue keep-awake\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=PATH={path}\n"
        f"Environment=XDG_STATE_HOME={state_home}\n"
        f"ExecStart={crr_bin} awake\n"
        "Restart=on-failure\n"
        f"RestartSec={restart_seconds}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def stop_awake_command() -> list[str]:
    """Stop the keep-awake loop, which IS how the hold is released."""
    return ["systemctl", "--user", "stop", AWAKE_SERVICE_NAME]
```

Add to `enable_commands()` and `disable_commands()` alongside
`WEB_SERVICE_NAME`, following the existing shape exactly.

In `crr/cli.py`'s `_cmd_systemd`, add to the `units` dict:

```python
        systemd.AWAKE_SERVICE_NAME: systemd.awake_service_unit(
            crr_bin, path, state_home,
            restart_seconds=config.get("web_restart_seconds"),
        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass. **Note:** `tests/test_cli.py` has tests asserting the
exact set of units `crr systemd` prints; update those to include the new
unit rather than deleting the assertion.

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/systemd.py crr/cli.py tests/test_systemd.py tests/test_cli.py
git commit -m "feat(systemd): crr-awake.service, its own unit for its own lifetime"
```

---

### Task 5: The launchd agent

**Files:**
- Modify: `crr/adapters/launchd.py`, `crr/cli.py`
- Test: `tests/test_launchd.py`

**Interfaces:**
- Produces: `launchd.AWAKE_LABEL`, `launchd.AWAKE_PLIST`, `launchd.awake_agent_plist(crr_bin, path)`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_launchd.py`, matching the file's existing
`plistlib`-parsing style rather than string matching:

```python
def test_awake_agent_runs_the_loop_and_keeps_it_alive():
    import plistlib
    parsed = plistlib.loads(
        launchd.awake_agent_plist("/opt/crr/bin/crr", "/usr/bin").encode("utf-8"))
    assert parsed["Label"] == launchd.AWAKE_LABEL
    assert parsed["ProgramArguments"] == ["/opt/crr/bin/crr", "awake"]
    assert parsed["KeepAlive"] is True
    assert parsed["EnvironmentVariables"]["PATH"] == "/usr/bin"


def test_awake_agent_has_no_start_interval():
    # It is a long-running loop, not a periodic job. A StartInterval would
    # spawn a second loop alongside the first, and two holders would fight.
    import plistlib
    parsed = plistlib.loads(
        launchd.awake_agent_plist("/opt/crr/bin/crr", "/usr/bin").encode("utf-8"))
    assert "StartInterval" not in parsed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_launchd.py -k awake -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Write minimal implementation**

Mirror `web_agent_plist` exactly — read it first and follow its structure,
label convention and plist-building approach. Add `AWAKE_LABEL` beside the
other labels and `AWAKE_PLIST = AWAKE_LABEL + ".plist"`. Wire it into
`_cmd_launchd`'s `agents` dict the same way the unit was wired in Task 4.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass. Update any `test_cli.py` assertion about the exact set
of agents `crr launchd` prints.

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/launchd.py crr/cli.py tests/test_launchd.py tests/test_cli.py
git commit -m "feat(launchd): crr-awake user agent"
```

---

### Task 6: `crr power` and the doctor lines

**Files:**
- Modify: `crr/cli.py`
- Test: `tests/test_power_consumer.py`

**Interfaces:**
- Consumes: everything above; `power.unmet`
- Produces: `cli._cmd_power(args) -> int`, `crr power [--release]`, and doctor output

- [ ] **Step 1: Write the failing test**

Append to `tests/test_power_consumer.py`:

```python
def test_power_reports_what_is_held_and_why(tmp_path, monkeypatch, capsys):
    holder = _FakeHolder()
    holder.hold(frozenset({"sleep"}), "crr: 2 Claude sessions live")
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 30, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    assert cli.main(["power"]) == 0
    out = capsys.readouterr().out
    assert "sleep" in out


def test_power_names_the_release_command_whenever_something_is_held(
        tmp_path, monkeypatch, capsys):
    # The block must never be a trap: if crr is holding the machine
    # awake, the way to stop it has to be on screen.
    holder = _FakeHolder()
    holder.hold(frozenset({"sleep"}), "r")
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: holder)
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 30, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "crr power --release" in out or "stop" in out


def test_power_reports_the_withheld_reason_when_nothing_is_held(
        tmp_path, monkeypatch, capsys):
    # "crr is holding nothing" is useless without the reason.
    monkeypatch.setattr(cli, "_power_holder", lambda *a, **k: _FakeHolder())
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(False))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep", "power_block_requires_ac": True,
                                 "power_poll_seconds": 30, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    cli.main(["power"])
    assert "battery" in capsys.readouterr().out


def test_power_states_capabilities_this_platform_lacks(tmp_path, monkeypatch, capsys):
    # macOS cannot block a shutdown. Silently holding half of what was
    # asked, and reporting success, is the failure this project keeps
    # finding.
    monkeypatch.setattr(cli, "_power_holder",
                        lambda *a, **k: _FakeHolder(caps=frozenset({"sleep"})))
    monkeypatch.setattr(cli, "_power_source", lambda *a, **k: _FakeSource(True))
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_config",
                        lambda: {"power_block": "sleep+shutdown",
                                 "power_block_requires_ac": True,
                                 "power_poll_seconds": 30, "power_block_max_hours": 12,
                                 "interop_timeout_seconds": 5})
    monkeypatch.setattr(cli, "_power_entries_and_owners", lambda *a, **k: ([{"pid": 1}], {1: [11]}))
    cli.main(["power"])
    out = capsys.readouterr().out
    assert "shutdown" in out and "unavailable" in out.lower()


def test_power_release_stops_the_unit_rather_than_pretending(
        tmp_path, monkeypatch, capsys):
    # The hold is a child of `crr awake`; this process has no handle to
    # it. Stopping the unit IS the release.
    ran = []
    monkeypatch.setattr(cli, "_run_commands", lambda cmds, label: ran.extend(cmds) or True)
    monkeypatch.setattr(cli.state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.host, "is_wsl", lambda: True)
    assert cli.main(["power", "--release"]) == 0
    assert ran and "stop" in " ".join(ran[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_power_consumer.py -k power_ -v`
Expected: FAIL — `argument command: invalid choice: 'power'`

- [ ] **Step 3: Write minimal implementation**

```python
def _cmd_power(args: argparse.Namespace) -> int:
    """Report what crr is holding, or stop the loop that holds it."""
    system = platform.system()
    wsl = host.is_wsl()
    config = _load_config()
    if args.release:
        # There is no handle to another process's child: stopping the loop
        # IS the release. Anything else would be a button that looks like
        # it did something.
        if system == "Darwin" and not wsl:
            cmds = [["launchctl", "bootout", f"gui/{os.getuid()}/{launchd.AWAKE_LABEL}"]]
        else:
            cmds = [systemd.stop_awake_command()]
        return 0 if _run_commands(cmds, "power") else 1

    try:
        holder = _power_holder(system, wsl, config.get("power_block_max_hours"))
    except NotImplementedError as exc:
        print(f"crr power: {exc}", file=sys.stderr)
        return 2
    source = _power_source(system, config.get("interop_timeout_seconds"))
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    store = JournalStore(state_dir.state_dir())
    entries, owners = _power_entries_and_owners(store, probe)
    live = _live_claude_count(entries, owners)
    requires_ac = bool(config.get("power_block_requires_ac"))
    on_ac = source.on_ac() if requires_ac else True
    decision = power.decide(live_sessions=live, on_ac=on_ac,
                            mode=str(config.get("power_block")),
                            requires_ac=requires_ac)
    held = holder.held()
    if held:
        print(f"holding: {', '.join(sorted(held))} — {decision.reason}")
        print("release with: crr power --release")
    else:
        print(f"holding: nothing — {decision.withheld or 'no reason recorded'}")
    missing = power.unmet(holder.capabilities(), decision.want)
    if missing:
        print(f"unavailable on this platform: {', '.join(missing)}")
    return 0
```

Register the subparser with `--release` (`action="store_true"`).

Then add to `_cmd_doctor`, after the boot-identity check, a block that
reports the same three facts (held / withheld / unavailable) using
`_check`, so `crr doctor` never omits an active hold.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/lint-imports`
Expected: all pass, contract kept

- [ ] **Step 5: Verify on this real host**

```bash
.venv/bin/crr power
.venv/bin/crr doctor | grep -i -A2 power
```

Expected: `holding: nothing — power_block is off` (this host's default), and
a doctor line saying the same. Neither should raise.

- [ ] **Step 6: Commit**

```bash
git add crr/cli.py tests/test_power_consumer.py
git commit -m "feat(cli): crr power, and a hold doctor can never omit"
```

---

## Self-Review

**Spec coverage.** `crr-awake` dedicated unit → Tasks 4, 5. Poll loop →
Tasks 2, 3. AC gating → Task 2. Config re-read / off switch → Task 3.
Visibility (doctor + a status command) → Task 6. Release semantics → Task 6,
with the reasoning stated in the plan header. `max_hours` reaching the
holder → Task 1 (a gap the phase-1a final review flagged).

**Deliberately deferred, and named in the spec:** the dashboard badge (needs
a `PAGE_VERSION` bump and a new sha pin, and belongs with the tray plan),
the Windows tray, `crr harden`.

**Placeholder scan.** Tasks 1–4 and 6 carry complete code. Task 5's Step 3
says "mirror `web_agent_plist`" rather than repeating a plist builder — the
existing function is the specification, its tests are shown in full, and
copying a 20-line XML builder into the plan would risk it drifting from the
file it must match.

**Type consistency.** `_power_poll_once(holder, source, entries, owners,
config)` and `_power_entries_and_owners(store, probe) -> (entries, owners)`
are used identically in Tasks 2, 3 and 6. `_power_holder(system, wsl,
max_hours=None)` matches phase 1a's two-arg call sites, which keep working
because the third argument is optional. `Decision.want`/`.reason`/`.withheld`
match `crr/core/power.py` as merged.
