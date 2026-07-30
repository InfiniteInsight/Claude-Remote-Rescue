# Audit Findings Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every defect and spec gap from the 2026-07-29 bug-hunt audit: dismissed-session resurrection, swallowed installer failures, over-broad kick kills, ungated detmux, sid path traversal, dead config keys, missing uninstall/restore/diagnostics-port, and doc drift.

**Architecture:** Each fix is a self-contained TDD task against the existing layered core (`crr.cli → crr.adapters → crr.core`). No new modules; changes land in existing files with their existing test files. Two tasks change `page.html` (each bumps `PAGE_VERSION` by one).

**Tech Stack:** Python 3.12 stdlib only, pytest, import-linter, node --check gate.

## Global Constraints

- **Layering (CI-enforced):** `crr.cli → crr.adapters → crr.core`. Core never imports adapters/cli. `.venv/bin/lint-imports` must print `KEPT`.
- **Zero runtime dependencies.** Never touch `[project.dependencies]`.
- **TDD, no exceptions:** write the failing test, run it, watch it fail for the right reason, then implement minimally.
- **Versioned contracts:** any stored/served shape change bumps its constant in `crr/core/contracts.py` (+ validator + test). Any `crr/core/page.html` change bumps `PAGE_VERSION` in `crr/core/web.py` by one.
- **Page-JS gate:** after any page.html/web.py change run `.venv/bin/pytest -k node` (node --check on every script block of the *rendered* page).
- **Untrusted fields reach the DOM via textContent only.**
- **Local CI green before every commit:** `.venv/bin/pytest -q` AND `.venv/bin/lint-imports`.
- **Commit trailer:** `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Work happens on branch `fix/audit-findings` (created before Task 1).
- Test runner: `.venv/bin/pytest`. Dev CLI: `.venv/bin/crr`.

---

### Task 1: Reviver must not resurrect dismissed sessions

**Files:**
- Modify: `crr/core/reviver.py:131` (the archive-pass skip tuple) and the section-2 comment at lines 125-129
- Test: `tests/test_reviver.py`

**Interfaces:**
- Consumes: existing `revive_crashed(...)` signature (unchanged).
- Produces: archived records with reason `"dismissed"` are never revival candidates. `"superseded-on-register"` and `"superseded-on-launch"` remain revivable (deliberate: their revival data was preserved *for* revival; only user-initiated dismissal is terminal). Record this decision in the updated comment.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_reviver.py` (mirror the existing `gave-up`/`detmuxed` skip tests in that file — reuse the same fixtures/fakes the neighboring tests use; the shape below shows intent, adapt fixture names to the file's existing helpers):

```python
def test_dismissed_archive_records_are_never_revived(tmp_path):
    """[bug 2026-07-29] dismiss archives with reason 'dismissed'; the reviver
    must treat that as terminal — resurrection un-dismisses the user's choice."""
    store, archive, boot, probe, tmux = _fixtures(tmp_path)  # same helpers the gave-up test uses
    entry = _crashed_entry(pid=101, sid="11111111-2222-3333-4444-555555555555")
    archive.archive(entry, "dismissed", NOW)
    outcome = reviver.revive_crashed(
        [], boot, probe, tmux, store, archive, max_strikes=3, now=NOW
    )
    assert outcome.revived == []
    assert tmux.spawned == []  # nothing spawned for the dismissed record
```

- [ ] **Step 2: Run it, watch it fail for the right reason**

Run: `.venv/bin/pytest tests/test_reviver.py -k dismissed -v`
Expected: FAIL — `outcome.revived == [101]` / a spawned session (the record WAS revived).

- [ ] **Step 3: Minimal fix**

In `crr/core/reviver.py` change line 131:

```python
        if record["reason"] in ("gave-up", "detmuxed", "dismissed"):
```

and extend the comment above it: `'dismissed' is the user's explicit "clean up
without restoring" — reviving it would un-dismiss their decision. The two
'superseded-*' reasons stay revivable on purpose: their archives exist to
preserve revival data.`

- [ ] **Step 4: Full local gates**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`
Expected: all pass, `KEPT`.

- [ ] **Step 5: Commit**

```bash
git add crr/core/reviver.py tests/test_reviver.py
git commit -m "fix(reviver): never resurrect dismissed sessions"
```

---

### Task 2: Installer failures must propagate (no green checkmarks on failure)

**Files:**
- Modify: `crr/cli.py` — `_cmd_systemd` (~line 916-922), `_cmd_launchd` (~954-960), `_cmd_schtasks` (~984-988); add one helper
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `systemd.enable_commands()`, `launchd.enable_commands(ad)`, `scheduled_task` command builders (unchanged).
- Produces: helper `_run_commands(cmds: list[list[str]], label: str) -> bool` in `crr/cli.py` (returns True iff every command ran and exited 0; prints each failure to stderr). Task 9 reuses it for uninstall.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (use the file's existing patterns for invoking `cli.main([...])` with `monkeypatch`; `Path.home` → `tmp_path` so unit files land in a sandbox):

```python
def test_systemd_install_failure_propagates(tmp_path, monkeypatch, capsys):
    """[bug 2026-07-29 / DESIGN lesson] a failed systemctl must not print the
    success line nor exit 0 — a swallowed exit code is a green checkmark."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=1)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.main(["systemd", "--install", "--crr-bin", "/usr/bin/crr"])
    out, err = capsys.readouterr()
    assert rc != 0
    assert "installed watchdog" not in out          # no success claim
    assert "exited 1" in err or "failed" in err     # failure surfaced
    assert calls                                     # commands were attempted

def test_systemd_install_success_still_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, returncode=0))
    rc = cli.main(["systemd", "--install", "--crr-bin", "/usr/bin/crr"])
    assert rc == 0
    assert "installed watchdog" in capsys.readouterr().out

def test_launchd_install_failure_propagates(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, returncode=1))
    rc = cli.main(["launchd", "--install", "--crr-bin", "/usr/bin/crr"])
    assert rc != 0
    assert "installed watchdog" not in capsys.readouterr().out

def test_schtasks_install_refuses_without_schtasks_exe(monkeypatch, capsys):
    """Off Windows/WSL the old code 'created' tasks it never created."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    rc = cli.main(["schtasks", "--install", "--crr-bin", "/usr/bin/crr"])
    assert rc != 0
    assert "created watchdog" not in capsys.readouterr().out
```

(`import subprocess` at the top of the test file if not present.)

- [ ] **Step 2: Run them, watch them fail for the right reason**

Run: `.venv/bin/pytest tests/test_cli.py -k "install_failure or refuses_without or install_success" -v`
Expected: the failure/refusal tests FAIL (rc == 0, success line printed); the success test may already pass.

- [ ] **Step 3: Implement**

Add the helper near `_quote` in `crr/cli.py`:

```python
def _run_commands(cmds: list[list[str]], label: str) -> bool:
    """Run each argv; surface every failure on stderr. True iff all exited 0.

    [lesson] a swallowed exit code turned hard failures into green
    checkmarks — install/uninstall must report what actually happened.
    """
    ok = True
    for cmd in cmds:
        shown = " ".join(cmd)
        try:
            result = subprocess.run(cmd, check=False)
        except OSError as exc:
            print(f"crr {label}: {shown} failed to run: {exc}", file=sys.stderr)
            ok = False
            continue
        if result.returncode != 0:
            print(f"crr {label}: {shown} exited {result.returncode}", file=sys.stderr)
            ok = False
    return ok
```

Rewrite the three install branches:

```python
    # _cmd_systemd
    if args.install:
        ud = systemd.unit_dir(Path.home())
        systemd.write_units(ud, units)
        if not _run_commands(systemd.enable_commands(), "systemd"):
            print(f"crr systemd: units written to {ud} but enabling FAILED (see above); "
                  "the watchdog/dashboard are NOT running", file=sys.stderr)
            return 1
        print(f"installed watchdog + dashboard units to {ud} and enabled them")
        return 0
```

```python
    # _cmd_launchd
    if args.install:
        ad = launchd.agent_dir(Path.home())
        launchd.write_agents(ad, agents)
        if not _run_commands(launchd.enable_commands(ad), "launchd"):
            print(f"crr launchd: agents written to {ad} but loading FAILED (see above); "
                  "the watchdog/dashboard are NOT running", file=sys.stderr)
            return 1
        print(f"installed watchdog + dashboard agents to {ad} and loaded them")
        return 0
```

```python
    # _cmd_schtasks
    if args.install:
        if shutil.which("schtasks.exe") is None:
            print("crr schtasks: schtasks.exe not found — not a Windows/WSL host; "
                  "nothing was created", file=sys.stderr)
            return 2
        if not _run_commands(cmds, "schtasks"):
            print("crr schtasks: task creation FAILED (see above)", file=sys.stderr)
            return 1
        print("created watchdog + dashboard Scheduled Tasks")
        return 0
```

- [ ] **Step 4: Full local gates**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit -m "fix(cli): propagate installer failures instead of printing success"
```

---

### Task 3: kick/close must target claude's group only, and partial kill failures must not roll back the flag

**Files:**
- Modify: `crr/adapters/process_probe.py` (`_ps_snapshot_argv`, `_parse_ps_rows`, `_child_groups`)
- Modify: `crr/core/ops.py` (`kick` lines ~163-170, `close` lines ~127-134)
- Test: `tests/test_adapters.py` (parse/selection), `tests/test_ops.py` (partial-failure flag semantics)

**Interfaces:**
- Consumes: `ProcessController.claude_groups(shell_pid) -> list[int]` port (name and signature unchanged; behavior now matches its name).
- Produces: `_parse_ps_rows(stdout) -> list[tuple[int, int, int, str]]` (pid, ppid, pgid, argv0). `_child_groups(rows, shell_pid)` selects only child groups whose direct child's argv0 **basename starts with `"claude"`** (covers `claude`, `claude-fake` test fakes, absolute paths; ancestry-scoped so this is not the banned global pattern-kill). `ops.kick`/`ops.close` keep the armed flag whenever ≥1 group kill landed.

- [ ] **Step 1: Write the failing adapter tests**

Add to `tests/test_adapters.py` (next to the existing `_child_groups` tests):

```python
def test_child_groups_selects_only_claude_children():
    """[bug 2026-07-29] kick killed every child group — a `make &` bg job died
    with the claude it was never part of. Selection is ancestry + argv0 basename
    prefix 'claude', never a global pattern."""
    rows = [
        (100, 1, 100, "-fish"),                       # the shell itself
        (200, 100, 200, "claude"),                    # claude child -> selected
        (300, 100, 300, "make"),                      # bg build -> NOT selected
        (400, 100, 400, "/usr/local/bin/claude"),     # abs path claude -> selected
        (500, 100, 500, "claude-fake"),               # test fake -> selected (prefix)
        (600, 200, 200, "node"),                      # grandchild, same group
    ]
    assert process_probe._child_groups(rows, 100) == [200, 400, 500]

def test_parse_ps_rows_with_args_column():
    out = "  100   1  100 -fish\n  200 100  200 claude --resume abc\n  bad line\n"
    assert process_probe._parse_ps_rows(out) == [
        (100, 1, 100, "-fish"),
        (200, 100, 200, "claude"),
    ]

def test_ps_snapshot_argv_includes_args():
    assert process_probe._ps_snapshot_argv() == ["ps", "-A", "-o", "pid=,ppid=,pgid=,args="]
```

Update any existing `_child_groups`/`_parse_ps_rows` tests in that file to the 4-tuple row shape, giving their claude-child rows an argv0 of `"claude"` (they pinned the old any-child behavior; the new contract is claude-only).

- [ ] **Step 2: Run, watch fail**

Run: `.venv/bin/pytest tests/test_adapters.py -k "child_groups or parse_ps or snapshot" -v`
Expected: FAIL (3-tuple rows / no filtering / old argv).

- [ ] **Step 3: Implement in `crr/adapters/process_probe.py`**

```python
def _ps_snapshot_argv() -> list[str]:
    # -A all processes; bare `=` headers -> no header line. args last so the
    # first three columns parse as ints and the remainder is the command line.
    return ["ps", "-A", "-o", "pid=,ppid=,pgid=,args="]


def _parse_ps_rows(stdout: str) -> list[tuple[int, int, int, str]]:
    rows: list[tuple[int, int, int, str]] = []
    for line in stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        argv0 = parts[3].split(None, 1)[0] if parts[3].strip() else ""
        rows.append((pid, ppid, pgid, argv0))
    return rows


def _is_claude_argv0(argv0: str) -> bool:
    """argv0 basename starts with 'claude' (claude, claude-fake, /path/claude).

    Scoped by ancestry (direct child of the journaled shell) — this is the
    claude-selection the port name promises, NOT a global cmdline pattern
    kill ([lesson: kill-by-ancestry] still holds).
    """
    base = argv0.rsplit("/", 1)[-1].lstrip("-")  # login shells prefix '-'
    return base.startswith("claude")


def _child_groups(rows: list[tuple[int, int, int, str]], shell_pid: int) -> list[int]:
    shell_pgid = next((pgid for pid, _ppid, pgid, _a in rows if pid == shell_pid), None)
    if shell_pgid is None:
        return []
    groups: list[int] = []
    for _pid, ppid, pgid, argv0 in rows:
        if (ppid == shell_pid and pgid != shell_pgid and pgid > 0
                and pgid not in groups and _is_claude_argv0(argv0)):
            groups.append(pgid)
    return groups
```

- [ ] **Step 4: Adapter tests green**

Run: `.venv/bin/pytest tests/test_adapters.py -v` — all pass (including the updated old tests).

- [ ] **Step 5: Write the failing ops tests (partial-failure flag semantics)**

Add to `tests/test_ops.py` (reuse its fake controller/flag fixtures; extend the fake controller so `terminate_group` can raise per-pgid):

```python
def test_kick_keeps_flag_when_any_group_kill_lands(fixtures):
    """[bug 2026-07-29] one landed kill + one OSError used to clear the flag —
    the wrapper then showed the crash prompt instead of silently resuming."""
    store, flags, boot, probe = fixtures
    controller = FakeController(groups=[200, 300], raise_for={300: OSError("gone")})
    res = ops.kick(store, controller, flags, boot, probe, PID, grace=0.1)
    assert res.ok
    assert flags.read(PID) is not None      # flag survives: a kill landed

def test_kick_clears_flag_when_no_kill_lands(fixtures):
    store, flags, boot, probe = fixtures
    controller = FakeController(groups=[200], raise_for={200: OSError("nope")})
    res = ops.kick(store, controller, flags, boot, probe, PID, grace=0.1)
    assert not res.ok
    assert flags.read(PID) is None          # nothing landed: flag rolled back
```

Mirror both for `ops.close`.

- [ ] **Step 6: Run, watch fail; then implement in `crr/core/ops.py`**

Replace the kill loop in `kick` (and identically in `close`, with its messages):

```python
    flags.arm_relaunch(pid, entry["claude"]["session_id"])
    landed, errors = 0, []
    for pgid in groups:
        try:
            controller.terminate_group(pgid, grace)
            landed += 1
        except OSError as exc:
            errors.append(str(exc))
    if landed == 0:
        flags.clear(pid)  # no kill landed -> the flag must not linger
        return OpResult(False, f"kick {pid} failed to signal: {'; '.join(errors)}")
    suffix = f" ({len(errors)} claude group(s) failed to signal: {'; '.join(errors)})" if errors else ""
    return OpResult(True, f"kicked {pid} (resuming the same conversation){suffix}")
```

(`close` success message stays `f"closed {pid}"` + the same suffix; its failure message `f"close {pid} failed to signal: ..."`.)

- [ ] **Step 7: Full local gates** — `.venv/bin/pytest -q && .venv/bin/lint-imports`. The e2e tests (`test_e2e_linux.py`) use `claude-fake` children; they must stay green with the new selection.

- [ ] **Step 8: Commit**

```bash
git add crr/adapters/process_probe.py crr/core/ops.py tests/test_adapters.py tests/test_ops.py
git commit -m "fix(kick/close): target only claude child groups; keep flag on partial kill"
```

---

### Task 4: detmux gets its classifier gate (op + button)

**Files:**
- Modify: `crr/core/ops.py` (`detmux` signature + gate), `crr/cli.py` (`_cmd_detmux`, web `action_provider`), `crr/core/page.html` (button gating, lines ~242-251), `crr/core/web.py` (`PAGE_VERSION` 8 → 9)
- Test: `tests/test_ops.py`, `tests/test_web.py` (if it pins button markup — check), `tests/test_cli.py` (if it invokes detmux — update call shape)

**Interfaces:**
- Consumes: `classify(entry, boot, probe)` from `crr.core.classifier`.
- Produces: `ops.detmux(store, archive, tmux, boot, probe, pid, now, *, tab_spawner)` — boot/probe inserted before `pid`, matching `dismiss`'s parameter order. Refuses non-CRASHED. De-tmux button renders only inside the `state === "crashed"` branch.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ops.py` (reuse the existing detmux fixtures; make the entry classify live — same boot id + alive pid — the way existing live-entry tests do):

```python
def test_detmux_refuses_live_session(detmux_fixtures):
    """[bug 2026-07-29] DESIGN: ALL session ops are classifier-gated. A live
    shell that inherited tmux_session via same-boot pid preservation must not
    be archived+delisted out of crr management."""
    store, archive, tmux, boot, probe, spawner = detmux_fixtures
    entry = _live_entry(pid=PID, tmux_session="crr-11111111")
    store.write(entry)
    res = ops.detmux(store, archive, tmux, boot, probe, PID, NOW, tab_spawner=spawner)
    assert not res.ok
    assert "not crashed" in res.message
    assert store.read(PID)                 # entry untouched
    assert spawner.opened == []            # no tab opened
```

Update existing detmux tests to the new signature with a boot/probe that classify the entry CRASHED.

- [ ] **Step 2: Run, watch fail** — `.venv/bin/pytest tests/test_ops.py -k detmux -v` (signature TypeError first is fine; after updating call sites in tests, the live-refusal assertion must be the failure).

- [ ] **Step 3: Implement**

`crr/core/ops.py` — new signature and gate right after the entry read:

```python
def detmux(
    store: JournalStore,
    archive: ArchiveStore,
    tmux: TmuxSpawner,
    boot: BootIdentity,
    probe: ProcessProbe,
    pid: int,
    now: str,
    *,
    tab_spawner: TabSpawner | None,
) -> OpResult:
    ...
    try:
        entry = store.read(pid)
    except (KeyError, contracts.ContractError):
        return OpResult(False, f"no session {pid}")
    state = classify(entry, boot, probe)
    if state != CRASHED:
        return OpResult(False, f"session {pid} is {state}, not crashed — refusing "
                               "(detmux re-homes revived sessions only)")
```

`crr/cli.py` `_cmd_detmux`: add boot detection + probe (copy the `_cmd_dismiss` pattern) and pass them; web `action_provider` detmux branch becomes `ops.detmux(store, archive, tmux_spawner, boot, probe, pid, _now(), tab_spawner=tab)`.

`crr/core/page.html` — move the De-tmux button inside the crashed branch:

```javascript
  if (s.state === "crashed") {
    addBtn("Reopen", "reopen", false);
    addBtn("Dismiss", "dismiss", false);
    if (s.tmux_session) {        // revived into tmux: offer re-homing
      addBtn("De-tmux", "detmux", false);
    }
  } else {                       // live or ghost: a running claude to act on
    addBtn("Kick", "kick", false);
    addBtn("Close", "close", true);
  }
```

`crr/core/web.py`: `PAGE_VERSION = 9  # v9: De-tmux gated to crashed cards; detmux classifier-gated`.

- [ ] **Step 4: Full local gates + page gate** — `.venv/bin/pytest -q && .venv/bin/lint-imports` (includes the node --check test).

- [ ] **Step 5: Commit**

```bash
git add crr/core/ops.py crr/cli.py crr/core/page.html crr/core/web.py tests/
git commit -m "fix(detmux): classifier-gate the op and the dashboard button"
```

---

### Task 5: Session-id shape validation (closes the path-traversal + glob injection)

**Files:**
- Modify: `crr/core/contracts.py` (add `valid_session_id` + enforce in journal/card validators), `crr/core/resume.py` (`derive_resume_sid` rejects non-uuid sids), `crr/core/archive.py` (`path_for` guard), `crr/cli.py` (`_cmd_claude_launch` guard)
- Test: `tests/test_contracts.py`, `tests/test_resume.py`, `tests/test_archive.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `contracts.valid_session_id(sid: Any) -> bool` (strict UUID: `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`). All crr-written sids are UUIDs (injected = `uuid.uuid4()`, guessed = transcript filename stems, which are claude's own UUIDs), so **no version bump**: the shape was always intended UUID; this pins it. Journal validator and session-card validator raise `ContractError` on non-UUID sids. `derive_resume_sid` returns `None` (untracked passthrough) for a non-UUID explicit sid and filters non-UUID transcript stems out of the guess pool — it must NEVER fall back to guessing when an explicit sid was given.

- [ ] **Step 1: Write the failing tests**

`tests/test_contracts.py`:

```python
def test_journal_rejects_path_traversal_sid():
    """[bug 2026-07-29] sid '../tabs/99' escaped the archive dir on write."""
    entry = _valid_entry()
    entry["claude"] = {"session_id": "../tabs/99", "sid_source": "guessed",
                       "started": "2026-07-30T00:00:00+00:00"}
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(entry)

def test_journal_rejects_glob_sid():
    entry = _valid_entry()
    entry["claude"] = {"session_id": "*", "sid_source": "guessed",
                       "started": "2026-07-30T00:00:00+00:00"}
    with pytest.raises(contracts.ContractError):
        contracts.validate_journal_entry(entry)

def test_valid_session_id_accepts_uuid_rejects_junk():
    assert contracts.valid_session_id("2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55")
    for bad in ("", "abc", "../x", "2f5c9a10", None, 42,
                "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55/../x"):
        assert not contracts.valid_session_id(bad)
```

`tests/test_resume.py`:

```python
def test_derive_rejects_non_uuid_explicit_sid_without_guessing():
    """A junk --resume arg must yield None (untracked), NEVER a confident
    guess of a different session."""
    transcripts = [{"session_id": "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55", "mtime": 5.0}]
    assert resume.derive_resume_sid("../tabs/99", transcripts) is None

def test_derive_ignores_non_uuid_transcript_stems():
    transcripts = [{"session_id": "not-a-uuid", "mtime": 9.0},
                   {"session_id": "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55", "mtime": 5.0}]
    sid, source = resume.derive_resume_sid(None, transcripts)
    assert sid == "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55" and source == "guessed"
```

`tests/test_archive.py`:

```python
def test_path_for_rejects_separators(tmp_path):
    store = ArchiveStore(tmp_path)
    with pytest.raises(contracts.ContractError):
        store.path_for("../tabs/99")
```

- [ ] **Step 2: Run, watch fail** — `.venv/bin/pytest tests/test_contracts.py tests/test_resume.py tests/test_archive.py -v`

- [ ] **Step 3: Implement**

`crr/core/contracts.py` (module already pure stdlib; add `import re` up top):

```python
_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def valid_session_id(sid: Any) -> bool:
    """True iff ``sid`` is a claude session UUID. Everything crr journals is
    one (injected uuid4 / transcript filename stems); anything else reaching a
    path or glob is an injection, not a session."""
    return isinstance(sid, str) and bool(_SESSION_ID_RE.match(sid))
```

In `validate_journal_entry`'s claude branch, after the `session_id` type check:

```python
        if not valid_session_id(claude["session_id"]):
            raise ContractError("journal 'claude.session_id' must be a UUID")
```

In `validate_session_card`, after the `session_id` type check (cards exist only for claude-bearing entries, so the sid is always supposed to be a UUID):

```python
    if not valid_session_id(card["session_id"]):
        raise ContractError("session 'session_id' must be a UUID")
```

`crr/core/resume.py` (`from crr.core import contracts` — core→core):

```python
def derive_resume_sid(explicit_sid: str | None, transcripts: _Transcripts):
    if explicit_sid:
        if not contracts.valid_session_id(explicit_sid):
            return None  # not a claude sid — pass through untracked, journal nothing
        known = {t["session_id"] for t in transcripts}
        return explicit_sid, ("verified" if explicit_sid in known else "guessed")
    candidates = [t for t in transcripts if contracts.valid_session_id(t["session_id"])]
    if not candidates:
        return None
    newest = max(candidates, key=lambda t: t["mtime"])
    return newest["session_id"], "guessed"
```

`crr/core/archive.py` `path_for` (belt-and-braces under the contract):

```python
    def path_for(self, session_id: str) -> Path:
        if not contracts.valid_session_id(session_id):
            raise contracts.ContractError(f"archive session_id is not a UUID: {session_id!r}")
        ...existing body...
```

`crr/cli.py` `_cmd_claude_launch` — a wrapper-forwarded `--session-id` the user typed may be junk; keep the wrapper's contract (always print the sid) but journal nothing:

```python
    sid = args.session_id or str(uuid.uuid4())
    if not contracts.valid_session_id(sid):
        print(sid)   # claude itself will reject it; we just don't journal junk
        return 0
    _attach_claude_session(state_dir.state_dir(), args.pid, sid, "injected")
    print(sid)
    return 0
```

Fix any existing test fixtures that use non-UUID sids (e.g. `"sid-123"`) — replace with UUID literals; that churn is expected and is the point of the pin.

- [ ] **Step 4: Full local gates** — `.venv/bin/pytest -q && .venv/bin/lint-imports`

- [ ] **Step 5: Commit**

```bash
git add crr/core/contracts.py crr/core/resume.py crr/core/archive.py crr/cli.py tests/
git commit -m "fix(contracts): pin session ids to UUID shape (closes archive path traversal)"
```

---

### Task 6: Dashboard poll/version intervals come from config (kill the magic numbers)

**Files:**
- Modify: `crr/core/page.html` (lines 142-143), `crr/core/web.py` (`render_page`, `handle_request`, `PAGE_VERSION` 9 → 10), `crr/cli.py` (`make_web_handler` + `_cmd_web` wiring)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `cfg.DEFAULTS["dashboard_poll_seconds"]`, `cfg.DEFAULTS["version_check_seconds"]` (`crr.core.config` — core→core import into web.py).
- Produces: `render_page(version=PAGE_VERSION, *, poll_seconds=None, version_check_seconds=None)` (None → the config DEFAULTS); `handle_request(..., poll_seconds=None, version_check_seconds=None)`; `make_web_handler(..., poll_seconds=None, version_check_seconds=None)`. Seconds→ms conversion happens in `render_page`.

- [ ] **Step 1: Write the failing tests**

`tests/test_web.py`:

```python
def test_render_page_substitutes_poll_intervals_from_defaults():
    """[audit P5] page intervals must be config, not magic numbers."""
    page = web.render_page()
    assert "@POLL_MS@" not in page and "@VERSION_MS@" not in page
    assert "var POLL_MS = 5000;" in page          # 5 s default * 1000
    assert "var VERSION_MS = 30000;" in page      # 30 s default * 1000

def test_render_page_honors_configured_intervals():
    page = web.render_page(poll_seconds=7, version_check_seconds=60)
    assert "var POLL_MS = 7000;" in page
    assert "var VERSION_MS = 60000;" in page

def test_handle_request_serves_configured_intervals():
    resp = web.handle_request(
        "GET", "/", {"Host": "127.0.0.1"},
        sessions_provider=lambda: {}, allowed_hosts={"127.0.0.1"},
        allowed_suffixes=(), poll_seconds=7, version_check_seconds=60,
    )
    assert b"var POLL_MS = 7000;" in resp.body
```

- [ ] **Step 2: Run, watch fail** — `.venv/bin/pytest tests/test_web.py -k interval -v`

- [ ] **Step 3: Implement**

`crr/core/page.html` lines 142-143:

```javascript
var POLL_MS = @POLL_MS@;
var VERSION_MS = @VERSION_MS@;
```

`crr/core/web.py` (add `from crr.core import config as cfg` up top):

```python
PAGE_VERSION = 10  # v10: poll/version intervals injected from config
_POLL_PLACEHOLDER = "@POLL_MS@"
_VERSION_MS_PLACEHOLDER = "@VERSION_MS@"


def render_page(
    version: int = PAGE_VERSION,
    *,
    poll_seconds: int | None = None,
    version_check_seconds: int | None = None,
) -> str:
    """Serve-time substitution of version + configured intervals into the page."""
    poll = cfg.DEFAULTS["dashboard_poll_seconds"] if poll_seconds is None else poll_seconds
    vchk = cfg.DEFAULTS["version_check_seconds"] if version_check_seconds is None else version_check_seconds
    return (
        load_page()
        .replace(_VERSION_PLACEHOLDER, str(version))
        .replace(_POLL_PLACEHOLDER, str(int(poll) * 1000))
        .replace(_VERSION_MS_PLACEHOLDER, str(int(vchk) * 1000))
    )
```

`handle_request`: add `poll_seconds: int | None = None, version_check_seconds: int | None = None` keyword params; the `path == "/"` branch becomes `render_page(page_version, poll_seconds=poll_seconds, version_check_seconds=version_check_seconds)`.

`crr/cli.py`: `make_web_handler` gains the same two optional params and forwards them into `web.handle_request`; `_cmd_web` passes `poll_seconds=config.get("dashboard_poll_seconds"), version_check_seconds=config.get("version_check_seconds")`.

Confirm the node-gate test renders via `render_page()` (defaults) so the checked JS has real numbers, not placeholders. If it checks `load_page()` raw, point it at `render_page()`.

- [ ] **Step 4: Full local gates** — `.venv/bin/pytest -q && .venv/bin/lint-imports`

- [ ] **Step 5: Commit**

```bash
git add crr/core/page.html crr/core/web.py crr/cli.py tests/test_web.py
git commit -m "feat(web): dashboard poll/version intervals from config (audit P5)"
```

---

### Task 7: Remove the three vestigial config keys (honest P5, not fake consumption)

**Files:**
- Modify: `crr/core/config.py` (DEFAULTS, `CONFIG_DEFAULTS_VERSION` 1 → 2), `DESIGN.md` (the audit-P5 paragraph, lines ~249-255)
- Test: `tests/test_config.py`

**Rationale (record in commit message + DESIGN):** `watcher_backoff_count`/`watcher_cooldown_seconds` describe ccresume's watcher backoff, which crr's reviver replaced with the strike-based give-up guard (`zombie_strikes`); `reopen_grace_seconds` describes ccresume's wait-for-tab-registration, which crr's tmux-first reopen made moot (revived tmux sessions run claude directly — nothing registers). A knob wired to nothing is a laundering worse than a magic number. Prerequisite: Task 6 already wired the two keys that DO have consumers.

- [ ] **Step 1: Write the failing tests**

Update `tests/test_config.py`: remove `reopen_grace_seconds`, `watcher_backoff_count`, `watcher_cooldown_seconds` from the expected-keys list; add:

```python
def test_vestigial_keys_are_gone_and_version_bumped():
    """[audit 2026-07-29] these keys had zero consumers — a knob wired to
    nothing is an invisible lie, not a prior."""
    for gone in ("reopen_grace_seconds", "watcher_backoff_count", "watcher_cooldown_seconds"):
        assert gone not in cfg.DEFAULTS
        with pytest.raises(cfg.ConfigError):
            cfg.Config({gone: 1})   # now an unknown key: loud, not silent
    assert cfg.CONFIG_DEFAULTS_VERSION == 2
```

- [ ] **Step 2: Run, watch fail** — `.venv/bin/pytest tests/test_config.py -v`

- [ ] **Step 3: Implement**

`crr/core/config.py`: delete the three lines from `DEFAULTS`; set `CONFIG_DEFAULTS_VERSION = 2` with the comment `# v2: dropped watcher_backoff_count / watcher_cooldown_seconds / reopen_grace_seconds (no crr mechanism consumes them; see DESIGN)`. First `grep -rn "watcher_backoff\|watcher_cooldown\|reopen_grace" crr/ tests/ docs/` and fix every remaining reference.

`DESIGN.md` audit-P5 paragraph: replace the caught-set list with the keys that exist, and append: *"ccresume's watcher backoff/cooldown and reopen tab-registration grace have no crr counterpart — the reviver's strike-based give-up guard and the tmux-first reopen replaced those mechanisms — so those knobs deliberately do not exist here (a knob wired to nothing is worse than a magic number)."*

- [ ] **Step 4: Full local gates** — `.venv/bin/pytest -q && .venv/bin/lint-imports`

- [ ] **Step 5: Commit**

```bash
git add crr/core/config.py tests/test_config.py DESIGN.md
git commit -m "fix(config): drop the three consumer-less keys; CONFIG_DEFAULTS_VERSION=2"
```

---

### Task 8: Guessed sids re-verify on status assembly, not only on revive

**Files:**
- Modify: `crr/cli.py` (`_cmd_status`, web `provider()`, plus a small pre-scan helper next to `_verify_guessed_sids`), `DESIGN.md` (line ~137 wording)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_verify_guessed_sids(store, now)` (exists, unchanged), `resume.verify_guessed`, `transcript_source.list_transcripts`.
- Produces: `_guessed_upgradable(store, now) -> bool` — a lock-free read-only pre-scan; the mutation lock is taken **only** when an upgrade will actually be written (the poll path stays lock-free in the common case).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py` (mirror the existing revive-path verification test's fixture technique — it already fakes `transcript_source.list_transcripts` via monkeypatch and builds a guessed entry with a `started` timestamp; reuse exactly that):

```python
def test_status_upgrades_guessed_sid_when_transcript_confirms(crr_state, monkeypatch, capsys):
    """[audit P3] 'stays guessed until a watchdog pass' — status itself must
    upgrade, so a dashboard without the watchdog still converges."""
    sid = "2f5c9a10-3e4b-4d6c-9f2a-1b7e8c0d4a55"
    _journal_guessed_entry(crr_state, pid=PID, sid=sid, started="2026-07-30T00:00:00+00:00")
    monkeypatch.setattr(cli.transcript_source, "list_transcripts",
                        lambda cwd, home=None: [{"session_id": sid, "mtime": 1e12}])
    rc = cli.main(["status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["sid_source"] == "verified"
```

- [ ] **Step 2: Run, watch fail** — expected: `sid_source == "guessed"`.

- [ ] **Step 3: Implement**

Add next to `_verify_guessed_sids` in `crr/cli.py`:

```python
def _guessed_upgradable(store: JournalStore, now: str) -> bool:
    """Lock-free pre-scan: would _verify_guessed_sids write anything?

    Keeps the poll path lock-free in the common case; the mutation lock is
    taken only when an upgrade is actually available to write.
    """
    by_cwd: dict[str, list] = {}
    for entry in store.scan().entries:
        claude = entry.get("claude")
        if not claude or claude.get("sid_source") != "guessed":
            continue
        cwd = entry["cwd"]
        if cwd not in by_cwd:
            by_cwd[cwd] = transcript_source.list_transcripts(cwd)
        if resume.verify_guessed(entry, by_cwd[cwd], now) is not None:
            return True
    return False
```

In `_cmd_status`, after building `store`/`probe` and before `store.scan()` for assembly:

```python
    sd = state_dir.state_dir()
    now = _now()
    if _guessed_upgradable(store, now):
        with mutation_lock(sd):
            _verify_guessed_sids(store, now)
```

(adjust: `store` is already built from `state_dir.state_dir()`; hoist `sd` so both uses share it). In `_cmd_web`'s `provider()`:

```python
    def provider() -> dict:
        now = _now()
        if _guessed_upgradable(store, now):
            with mutation_lock(sd):
                _verify_guessed_sids(store, now)
        payload = status.assemble_sessions(store.scan().entries, boot, probe, tail_facts=extract)
        contracts.validate_sessions_payload(payload)
        return payload
```

`DESIGN.md` ~line 137: change "The wrapper re-verifies guessed sids after launch and upgrades them" to "Guessed sids are re-verified against their transcript on every status assembly and every revive sweep, and upgraded to `verified` once the transcript confirms them".

- [ ] **Step 4: Full local gates** — `.venv/bin/pytest -q && .venv/bin/lint-imports`

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_cli.py DESIGN.md
git commit -m "feat(status): re-verify guessed sids on status assembly (audit P3)"
```

---

### Task 9: `restore` alias + `--uninstall` for all three service managers

**Files:**
- Modify: `crr/cli.py` (reopen parser alias; `--uninstall` on systemd/launchd/schtasks), `crr/adapters/systemd.py` (`disable_commands`), `crr/adapters/launchd.py` (`disable_commands`)
- Test: `tests/test_cli.py`, `tests/test_systemd.py`, `tests/test_launchd.py`

**Interfaces:**
- Consumes: `_run_commands` from Task 2; `scheduled_task.delete_task_commands()` (exists).
- Produces: `systemd.disable_commands() -> list[list[str]]`, `launchd.disable_commands(target_dir: Path) -> list[list[str]]`. CLI flags `--uninstall` (mutually exclusive with `--install`). `crr restore --pid N` behaves exactly as `crr reopen --pid N`.

- [ ] **Step 1: Write the failing tests**

`tests/test_systemd.py`:

```python
def test_disable_commands_mirror_enable():
    assert systemd.disable_commands() == [
        ["systemctl", "--user", "disable", "--now", systemd.TIMER_NAME],
        ["systemctl", "--user", "disable", "--now", systemd.WEB_SERVICE_NAME],
        ["systemctl", "--user", "daemon-reload"],
    ]
```

`tests/test_launchd.py`:

```python
def test_disable_commands_unload_both_agents(tmp_path):
    cmds = launchd.disable_commands(tmp_path)
    assert cmds == [
        ["launchctl", "unload", "-w", str(tmp_path / launchd.REVIVE_PLIST)],
        ["launchctl", "unload", "-w", str(tmp_path / launchd.WEB_PLIST)],
    ]
```

`tests/test_cli.py`:

```python
def test_restore_is_an_alias_for_reopen(monkeypatch):
    """DESIGN names the op 'reopen/restore'; both must parse."""
    seen = {}
    monkeypatch.setattr(cli, "_cmd_reopen", lambda args: seen.setdefault("pid", args.pid) or 0)
    assert cli.main(["restore", "--pid", "424242"]) == 0
    assert seen["pid"] == 424242

def test_systemd_uninstall_disables_and_removes_units(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ud = tmp_path / ".config" / "systemd" / "user"
    ud.mkdir(parents=True)
    for name in (cli.systemd.SERVICE_NAME, cli.systemd.TIMER_NAME, cli.systemd.WEB_SERVICE_NAME):
        (ud / name).write_text("x")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    rc = cli.main(["systemd", "--uninstall", "--crr-bin", "/usr/bin/crr"])
    assert rc == 0
    assert ["systemctl", "--user", "disable", "--now", cli.systemd.TIMER_NAME] in ran
    assert not any((ud / n).exists() for n in
                   (cli.systemd.SERVICE_NAME, cli.systemd.TIMER_NAME, cli.systemd.WEB_SERVICE_NAME))

def test_systemd_uninstall_failure_propagates(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1))
    assert cli.main(["systemd", "--uninstall", "--crr-bin", "/usr/bin/crr"]) != 0
```

- [ ] **Step 2: Run, watch fail** — alias test fails with argparse error; disable_commands with AttributeError.

- [ ] **Step 3: Implement**

`crr/adapters/systemd.py`:

```python
def disable_commands() -> list[list[str]]:
    """The commands that deactivate the watchdog + dashboard (data, not run).

    Mirror of enable_commands; linger is left alone (other services may
    rely on it — enabling it was additive, so removal is the user's call).
    """
    return [
        ["systemctl", "--user", "disable", "--now", TIMER_NAME],
        ["systemctl", "--user", "disable", "--now", WEB_SERVICE_NAME],
        ["systemctl", "--user", "daemon-reload"],
    ]
```

`crr/adapters/launchd.py`:

```python
def disable_commands(target_dir: Path) -> list[list[str]]:
    """The commands that unload both agents (data, not run)."""
    target_dir = Path(target_dir)
    return [
        ["launchctl", "unload", "-w", str(target_dir / REVIVE_PLIST)],
        ["launchctl", "unload", "-w", str(target_dir / WEB_PLIST)],
    ]
```

`crr/cli.py`:
- reopen parser: `reo = sub.add_parser("reopen", aliases=["restore"], help="revive one specific crashed session now (alias: restore)")`.
- Each of the three service parsers gains `--uninstall` (`action="store_true"`, help "disable/remove the watchdog + dashboard integration"). At the top of each `_cmd_*`: `if args.install and args.uninstall: print(..., file=sys.stderr); return 2`.
- systemd uninstall branch (before the install branch):

```python
    if args.uninstall:
        ud = systemd.unit_dir(Path.home())
        ok = _run_commands(systemd.disable_commands(), "systemd")
        for name in (systemd.SERVICE_NAME, systemd.TIMER_NAME, systemd.WEB_SERVICE_NAME):
            (ud / name).unlink(missing_ok=True)
        if not ok:
            print("crr systemd: unit files removed, but disabling FAILED (see above)",
                  file=sys.stderr)
            return 1
        print(f"uninstalled watchdog + dashboard units from {ud}")
        return 0
```

- launchd uninstall branch: same shape with `launchd.disable_commands(ad)` and the two plists (note: unload first, THEN remove the files — launchctl needs the plist to unload).
- schtasks uninstall branch: preflight `shutil.which("schtasks.exe")` (return 2 with the not-a-WSL-host message), then `_run_commands(scheduled_task.delete_task_commands(), "schtasks")`, propagate, success message `"removed watchdog + dashboard Scheduled Tasks"`.

- [ ] **Step 4: Full local gates** — `.venv/bin/pytest -q && .venv/bin/lint-imports`

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py crr/adapters/systemd.py crr/adapters/launchd.py tests/
git commit -m "feat(cli): restore alias + --uninstall for systemd/launchd/schtasks"
```

---

### Task 10: DiagnosticsSource port + doc drift cleanup

**Files:**
- Modify: `crr/core/ports.py` (add protocol), `crr/cli.py` (annotate `gather_diagnostics`), `crr/core/config.py` (docstring only), `README.md` (command table), `DESIGN.md` (line ~176 badge list), `CHANGELOG.md` (Unreleased section)
- Test: `tests/test_adapters.py`

**Interfaces:**
- Consumes: the three diagnostics adapter modules' existing `SOURCE_NAME`/`available`/`collect`.
- Produces: `crr.core.ports.DiagnosticsSource` Protocol. Adapters are *modules* satisfying it structurally; the test checks conformance by attribute, not isinstance.

- [ ] **Step 1: Write the failing test**

`tests/test_adapters.py`:

```python
def test_all_diagnostics_sources_satisfy_the_port():
    """DESIGN: diagnostics is an adapter interface. The de-facto contract
    (SOURCE_NAME / available / collect) is now a declared core port."""
    from crr.core.ports import DiagnosticsSource
    for module in (diagnostics, diagnostics_macos, diagnostics_windows):
        assert isinstance(module.SOURCE_NAME, str) and module.SOURCE_NAME
        assert callable(module.available)
        assert callable(module.collect)
        sig = inspect.signature(module.collect)
        assert len(sig.parameters) == 1   # collect(config)
```

(add the needed imports at the top of the test file: `import inspect`, the two extra diagnostics modules.)

- [ ] **Step 2: Run, watch fail** — ImportError on `DiagnosticsSource`.

- [ ] **Step 3: Implement**

`crr/core/ports.py` (follow the file's existing Protocol style):

```python
class DiagnosticsSource(Protocol):
    """Platform "why did it die" source (journald / log+pmset / winevent).

    Implemented by adapter *modules* (crr.adapters.diagnostics*), not
    classes. ``collect(config)`` returns
    ``(boots, prev_boot_errors, host_events, degraded)`` and degrades
    per-source rather than raising.
    """

    SOURCE_NAME: str

    def available(self) -> bool: ...

    def collect(self, config: Any) -> tuple[list, list, list, list]: ...
```

(`Any` avoids widening ports' imports; the real type is `crr.core.config.Config` — say so in the docstring.)

`crr/cli.py`: annotate `def gather_diagnostics(config: cfg.Config, source: "ports.DiagnosticsSource | None" = None)` — import `ports` from `crr.core` (already-allowed direction) or reference via existing imports.

Docs:
- `crr/core/config.py` docstring: delete the stale "TOML file loading is intentionally not here yet" paragraph; replace with one sentence: "TOML loading lives here too (``load_toml_overrides``); ``Config`` still takes a plain overrides mapping so tests need no files."
- `README.md` command table: add rows for `crr schtasks` (Windows/WSL Scheduled Tasks), `crr shim <shell>` (print the shell shim), `crr repair-check` ([shim] flag read), `crr restore` (alias of reopen), and mention `--uninstall` on the three service commands and `crr config --effective`.
- `DESIGN.md` line ~176: `state badges (ghost/crashed/idle/duplicate)` → `state badges (ghost/crashed/duplicate)` (the classifier is three-state; `idle` never existed).
- `CHANGELOG.md`: under an `## [Unreleased]` heading (create if absent) add Fixed entries (dismissed-resurrection, installer exit codes, kick group selection, detmux gate, sid UUID pin) and Added entries (config-driven poll intervals, restore alias, --uninstall, DiagnosticsSource port, status-time sid verification, CONFIG_DEFAULTS_VERSION 2 key removals).

- [ ] **Step 4: Full local gates** — `.venv/bin/pytest -q && .venv/bin/lint-imports`

- [ ] **Step 5: Commit**

```bash
git add crr/core/ports.py crr/cli.py crr/core/config.py README.md DESIGN.md CHANGELOG.md tests/test_adapters.py
git commit -m "feat(ports): declare DiagnosticsSource; fix doc drift (README/DESIGN/config)"
```

---

## Out of scope (deliberately, report to user)

- **Docs site** (ROADMAP Phase 5) and **restore-prompt UX parity** (Phase 3) — roadmap features, not defects.
- **macOS/Windows hardware verification** — already honestly declared UNVERIFIED in README; no code change can substitute.
- The contracts "debug mode" toggle (P7) — validation is unconditionally on at both surfaces, which is strictly stronger than a debug flag.

## Self-review notes

- Task ordering matters twice: Task 6 (wire poll keys) MUST precede Task 7 (remove the other keys) so the key list in test_config is edited once coherently; Task 2's `_run_commands` is consumed by Task 9.
- PAGE_VERSION: Task 4 → 9, Task 6 → 10. Each bumps by exactly one in its own commit.
- Type consistency: `_parse_ps_rows` 4-tuples are consumed only inside `process_probe.py` (`_child_groups`); `PsProcessController.claude_groups` public signature unchanged. `ops.detmux` new signature is updated at BOTH call sites (`_cmd_detmux`, web `action_provider`) in Task 4.
- All sid literals in tests use full UUIDs after Task 5; earlier tasks (1) also use UUID sids so they survive Task 5 unchanged.
