# Reopen Tab Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sessions should always have a tab on the laptop — both from the phone dashboard and after reboots.

**Architecture:** Two narrow fixes to existing paths. (1) Extend `ops.reopen` to handle LIVE sessions with a `tmux_session` by calling `_open_tab` instead of refusing, and add a Reopen button to LIVE cards in page.html. (2) Remove the [Y/n] prompt from `_rescue_check` so tabs auto-open on the first interactive shell after reboot, gated by a new `rescue_auto_open` config key.

**Tech Stack:** Python (stdlib), JavaScript (inline in page.html), pytest

## Global Constraints

- Zero runtime dependencies — stdlib only
- One-way layering: `crr.cli` → `crr.adapters` → `crr.core`; `crr.core` never imports `crr.adapters` or `crr.cli`
- No test may attach real tmux, register real scheduled tasks, write HKLM, reboot, or exec (monkeypatch `_exec = os.execvp` seam in every test reaching it)
- TDD: tests first, implementation second
- `PAGE_VERSION` must bump and a new pin must be appended in `tests/test_page_version_guard.py` for every `page.html` change
- `CONFIG_DEFAULTS_VERSION` must bump (20 → 21) with a ledger comment for every new config key
- `textContent` for untrusted fields, `setAttribute("href", ...)` for links in page.html

---

### Task 1: Extend `ops.reopen` to open a tab on LIVE sessions

**Files:**
- Modify: `crr/core/ops.py:131-139` (the LIVE guard in `reopen()`)
- Test: `tests/test_ops.py` (add new tests after the existing reopen block, around line 373)

**Interfaces:**
- Consumes: existing `_open_tab(tab_spawner, name) -> tuple[str, bool]` at `ops.py:634`
- Consumes: existing `OpResult(ok, message, degraded)` at `ops.py:30`
- Produces: `ops.reopen()` now returns `OpResult(True, ...)` for LIVE sessions with a `tmux_session` field, instead of refusing

- [ ] **Step 1: Write failing tests**

Add these tests in `tests/test_ops.py` after the `test_opresult_defaults_to_not_degraded` test (around line 373). They test the new LIVE-with-tmux tab-attach behavior:

```python
# --- reopen LIVE-with-tmux (tab attach, no revival) -----------------------

def test_reopen_live_with_tmux_opens_tab(tmp_path):
    """A LIVE session with a tmux_session gets a tab, not a refusal."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="same-boot", claude=_claude())
    name = f"crr-{_SID}"
    # Write tmux_session into the entry
    entry = store.read(42)
    entry["tmux_session"] = name
    store.write(entry)
    tmux = FakeTmux(live={name}, session_pids={name: 42})
    tab = FakeTabSpawner()
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot("same-boot"),
                     FakeProbe(alive=True, tty=True), 42, _NOW,
                     grace=0.1, remote_control=True,
                     tab_spawner=tab, tabs_expected=True)
    assert res.ok is True
    assert tab.opened  # a tab was opened
    assert tmux.created == []  # no new tmux session spawned


def test_reopen_live_without_tmux_still_refused(tmp_path):
    """A LIVE session with NO tmux_session is still refused (no tab target)."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="same-boot", claude=_claude())
    # No tmux_session field set — bare live shell
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, FakeTmux(), ctrl, flags, FakeBoot("same-boot"),
                     FakeProbe(alive=True, tty=True), 42, _NOW,
                     grace=0.1, remote_control=True)
    assert not res.ok
    assert "is live" in res.message


def test_reopen_live_with_tmux_degraded_when_tab_fails(tmp_path):
    """Tab failure on a LIVE-with-tmux is degraded, not a hard failure."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="same-boot", claude=_claude())
    name = f"crr-{_SID}"
    entry = store.read(42)
    entry["tmux_session"] = name
    store.write(entry)
    tmux = FakeTmux(live={name}, session_pids={name: 42})
    tab = FakeTabSpawner(fail=True)
    ctrl, flags = _idle_ctrl_flags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot("same-boot"),
                     FakeProbe(alive=True, tty=True), 42, _NOW,
                     grace=0.1, remote_control=True,
                     tab_spawner=tab, tabs_expected=True)
    assert res.ok is True
    assert res.degraded is True
    assert "tmux attach -t" in res.message


def test_reopen_live_with_tmux_no_spawn_no_archive(tmp_path):
    """LIVE-with-tmux must not spawn, kill, archive, or touch flags."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, boot="same-boot", claude=_claude())
    name = f"crr-{_SID}"
    entry = store.read(42)
    entry["tmux_session"] = name
    store.write(entry)
    tmux = FakeTmux(live={name}, session_pids={name: 42})
    tab = FakeTabSpawner()
    ctrl, flags = FakeController(groups=[200]), FakeFlags()
    res = ops.reopen(store, archive, tmux, ctrl, flags, FakeBoot("same-boot"),
                     FakeProbe(alive=True, tty=True), 42, _NOW,
                     grace=0.1, remote_control=True,
                     tab_spawner=tab, tabs_expected=True)
    assert res.ok
    assert tmux.created == []
    assert ctrl.terminated == []
    assert flags.armed == {}
    assert archive.scan().records == []
    assert store.read(42)  # untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ops.py::test_reopen_live_with_tmux_opens_tab tests/test_ops.py::test_reopen_live_without_tmux_still_refused tests/test_ops.py::test_reopen_live_with_tmux_degraded_when_tab_fails tests/test_ops.py::test_reopen_live_with_tmux_no_spawn_no_archive -v`

Expected: `test_reopen_live_with_tmux_opens_tab` FAILS (currently returns `ok=False`, "is live"). The "without_tmux_still_refused" test should PASS (existing behavior). The degraded and no-spawn tests FAIL.

- [ ] **Step 3: Implement the LIVE-with-tmux branch in `ops.reopen`**

In `crr/core/ops.py`, replace the LIVE guard block (lines 131-139):

```python
    if state == LIVE:
        # A LIVE entry is normally a shell the user is working in, and
        # reopen would race a spawn against it. A PARKED one is different:
        # the journaled pid IS the process in the tmux session (#58), so
        # there is nothing to race and reopen means what the user expects —
        # attach a tab to it. Anything else live is still refused.
        name = entry.get("tmux_session")
        if not (name and name in live and tmux.session_pid(name) == pid):
            return OpResult(False, f"session {pid} is live — use kick or close")
```

With:

```python
    if state == LIVE:
        name = entry.get("tmux_session")
        if name and name in live:
            suffix, landed = _open_tab(tab_spawner, name)
            return OpResult(True, f"opened tab for {name}" + suffix,
                            degraded=tabs_expected and not landed)
        return OpResult(False, f"session {pid} is live — use kick or close")
```

This changes the logic: any LIVE session with a `tmux_session` present in the live set gets tab-attach-only. No spawn, no kill, no archive. Sessions without `tmux_session` (or whose tmux session isn't live) are still refused.

Note: the old PARKED sub-case check (`tmux.session_pid(name) == pid`) verified that the journaled pid owned the tmux session. The new code drops that check — if the tmux session exists and the journal says it belongs to this entry, opening a tab is safe regardless of pid ownership. The session is alive and the name is right.

- [ ] **Step 4: Update the docstring**

Update the `reopen()` docstring (line 106) from:

```
    - LIVE: refused — kick/close are the ops for a running claude.
```

To:

```
    - LIVE with tmux_session: tab-attach-only (no revival, no kill, no
      archive). Subsumes the PARKED path — any live session with a tmux
      home gets a tab.
    - LIVE without tmux_session: refused — kick/close are the ops for
      a running claude with no tmux target to attach to.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_ops.py -v -k reopen`

Expected: ALL reopen tests PASS, including the four new ones and all existing ones. The old `test_reopen_live_refused` test (line 175) still passes — it seeds a LIVE entry with NO `tmux_session`, so the refusal holds.

Check that `test_reopen_opens_a_tab_even_when_already_running` (line 233) still passes — it's a CRASHED entry that already has a running tmux session (the session was revived in a previous pass). It takes the CRASHED branch, not the new LIVE branch.

- [ ] **Step 6: Run the full test suite**

Run: `pytest`

Expected: all tests pass, no regressions.

- [ ] **Step 7: Commit**

```bash
git add crr/core/ops.py tests/test_ops.py
git commit -m "feat(ops): extend reopen to open a tab on LIVE sessions with tmux_session"
```

---

### Task 2: Add `rescue_auto_open` config key

**Files:**
- Modify: `crr/core/config.py:93-99` (version bump + new key in DEFAULTS)
- Modify: `tests/test_config.py:80-88` (version pin bump + new default test)

**Interfaces:**
- Produces: `config.get("rescue_auto_open")` returns `True` by default. `CONFIG_DEFAULTS_VERSION` is `21`.

- [ ] **Step 1: Write failing tests**

Add a test for the new default in `tests/test_config.py`, after the existing rescue prompt test (around line 30):

```python
def test_rescue_auto_open_default():
    assert cfg.DEFAULTS["rescue_auto_open"] is True
    assert cfg.Config().get("rescue_auto_open") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_rescue_auto_open_default -v`

Expected: FAIL — `KeyError: 'rescue_auto_open'`

- [ ] **Step 3: Add the config key and bump version**

In `crr/core/config.py`:

1. Add the ledger comment after the v20 line (after line 94):

```python
# v21: added rescue_auto_open (spec 2026-08-20 — reopen tab reliability:
# auto-open tabs for restored sessions on boot, skipping the [Y/n] prompt)
```

2. Bump the version (line 95):

```python
CONFIG_DEFAULTS_VERSION = 21
```

3. Add the key to DEFAULTS, after the `rescue_prompt_timeout_seconds` entry (after line 131):

```python
    "rescue_auto_open": True,        # skip [Y/n] and auto-open tabs for restored sessions
```

- [ ] **Step 4: Update the version pin test**

In `tests/test_config.py`, update `test_vestigial_keys_are_gone_and_version_bumped` (line 87-88):

```python
    # v21 (2026-08-20): rescue_auto_open (spec — reopen tab reliability: auto-open on boot).
    assert cfg.CONFIG_DEFAULTS_VERSION == 21
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`

Expected: ALL config tests PASS.

- [ ] **Step 6: Run the full test suite**

Run: `pytest`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add crr/core/config.py tests/test_config.py
git commit -m "feat(config): add rescue_auto_open (default true), bump version 20→21"
```

---

### Task 3: Auto-open tabs in `_rescue_check` when `rescue_auto_open` is true

**Files:**
- Modify: `crr/cli.py:3515-3597` (the `_rescue_check` function)
- No new test file — `_rescue_check` is an integration-level CLI function that calls real tmux/boot_identity; it's guarded by `_cmd_rescue_check`'s blanket exception handler (line 3511). The behavioral change is: when `rescue_auto_open` is true, skip the `_rescue_prompt_yes` call and go straight to opening tabs. When false, use the existing prompt. The ops.reopen and config paths are individually tested by Tasks 1 and 2.

**Interfaces:**
- Consumes: `config.get("rescue_auto_open")` from Task 2 (returns `True` by default)
- Consumes: `ops.reopen()` LIVE-with-tmux behavior from Task 1 (the rescue-check calls `ops.reopen` for each found session)

- [ ] **Step 1: Modify `_rescue_check` to support auto-open**

In `crr/cli.py`, modify the `_rescue_check` function. There are two places where `_rescue_prompt_yes` is called:

**First call (headless, `not tabs_expected`, line 3562):** Change from:

```python
    if not tabs_expected:
        # Genuinely headless (no GUI tabs on this host); we have a tty (this
        # function is tty-gated up top). Offer the tmux-window path (#headless).
        if not _rescue_prompt_yes(config, n):
            print("not now — 'crr rescued' lists them")
            return 0
        sessions = [(e["tmux_session"], _win_label(e["cwd"])) for e in found]
        _terminal_reopen(sessions, config, sd)  # may exec (replaces this process)
        return 0
```

To:

```python
    if not tabs_expected:
        if not config.get("rescue_auto_open"):
            if not _rescue_prompt_yes(config, n):
                print("not now — 'crr rescued' lists them")
                return 0
        sessions = [(e["tmux_session"], _win_label(e["cwd"])) for e in found]
        _terminal_reopen(sessions, config, sd)  # may exec (replaces this process)
        return 0
```

**Second call (GUI-capable host, line 3576):** Change from:

```python
    if not _rescue_prompt_yes(config, n):
        print("not now — 'crr rescued' lists them")
        return 0
```

To:

```python
    if not config.get("rescue_auto_open"):
        if not _rescue_prompt_yes(config, n):
            print("not now — 'crr rescued' lists them")
            return 0
```

The rest of the function (the `ops.reopen` loop at line 3582-3596) stays unchanged — it already opens tabs via `tab_spawner`.

- [ ] **Step 2: Run the full test suite**

Run: `pytest`

Expected: all tests pass. No test directly exercises `_rescue_check` (it requires real tmux/boot_identity), but the full suite confirms no regressions.

- [ ] **Step 3: Commit**

```bash
git add crr/cli.py
git commit -m "feat(rescue): auto-open tabs on boot when rescue_auto_open is true"
```

---

### Task 4: Add Reopen button to LIVE cards in page.html

**Files:**
- Modify: `crr/core/page.html:907-912` (the `else` block for live/ghost states)
- Modify: `crr/core/web.py:44` (bump `PAGE_VERSION` from 58 to 59)
- Modify: `tests/test_page_version_guard.py:37-52` (add pin for v59)
- Modify: `tests/test_web.py:932` (rename version test to `test_page_version_is_59`)

**Interfaces:**
- Consumes: `s.tmux_session` field on session cards (always present in the status payload, see `status.py:277`)
- Consumes: `ops.reopen()` LIVE-with-tmux behavior from Task 1 (the dashboard calls `action_provider("reopen", pid)` which calls `ops.reopen`)

- [ ] **Step 1: Add Reopen button to the LIVE state action set**

In `crr/core/page.html`, modify the `else` block at line 907. Change from:

```javascript
  } else {                       // live or ghost: a running claude to act on
    if (s.state === "ghost") {   // orphaned shell: offer mobile rescue
      addBtn("Restore", "reopen", false);
    }
    addBtn("Kick", "kick", false);
    addBtn("Close", "close", true);
  }
```

To:

```javascript
  } else {                       // live or ghost: a running claude to act on
    if (s.state === "ghost") {   // orphaned shell: offer mobile rescue
      addBtn("Restore", "reopen", false);
    } else if (s.tmux_session) { // live with tmux: offer tab attach
      addBtn("Reopen", "reopen", false, false);
    }
    addBtn("Kick", "kick", false);
    addBtn("Close", "close", true);
  }
```

The guard `s.tmux_session` ensures the button only appears when there is a tmux session to attach to. The `else if` ensures it doesn't double up with Ghost's "Restore" button.

- [ ] **Step 2: Bump PAGE_VERSION**

In `crr/core/web.py`, change line 44:

```python
PAGE_VERSION = 59  # v59: Reopen button on LIVE cards with tmux_session
```

- [ ] **Step 3: Add page version pin**

First, compute the sha256 of the modified page.html:

```bash
python3 -c "import hashlib; print(hashlib.sha256(open('crr/core/page.html','rb').read()).hexdigest())"
```

Then in `tests/test_page_version_guard.py`, prepend the new entry to `PAGE_PINS` (after line 37):

```python
PAGE_PINS: dict[int, str] = {
    59: "<computed sha256>",
    58: "acaef00dba29842887e481c89b369321b5b3d5ea35880cf4aca8ceffe4e1d0dc",
    # ... rest unchanged
```

- [ ] **Step 4: Update version name test**

In `tests/test_web.py`, rename the version test (line 932) from `test_page_version_is_58` to `test_page_version_is_59` and update its assertion:

```python
def test_page_version_is_59():
    assert web.PAGE_VERSION == 59
```

- [ ] **Step 5: Run page version and web tests**

Run: `pytest tests/test_page_version_guard.py tests/test_web.py -v`

Expected: ALL PASS.

- [ ] **Step 6: Run the full test suite**

Run: `pytest`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add crr/core/page.html crr/core/web.py tests/test_page_version_guard.py tests/test_web.py
git commit -m "feat(dashboard): add Reopen button to LIVE cards with tmux_session"
```
