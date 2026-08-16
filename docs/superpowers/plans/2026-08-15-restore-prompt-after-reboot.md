# Restore-Prompt After a Reboot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `crr rescue-check` actually fire after a reboot — offer to open the conversations crr restored (parked in tmux, not yet opened) in tabs, and keep them tracked.

**Architecture:** Two small changes to the existing mechanism. (1) `rescue.rescued_sessions()` stops keying on `boot_id != current_boot` (which the reviver's #58 re-key defeats — it stamps the current boot on every restore) and instead selects entries **parked in a live tmux session but not attached** — reusing `tmux.attached_sessions()` from #32. (2) The `[Y]` path calls `ops.reopen` (attach a tab, keep the entry tracked) instead of `ops.detmux` (attach, then untrack). Once-per-boot marker, tty gate, headless notice, and timeout behavior are unchanged.

**Tech Stack:** Python 3.12, stdlib only; pytest; the crr one-way layering (`crr.cli → crr.adapters → crr.core`).

## Global Constraints

- Runtime dependencies stay ZERO (stdlib only). One-way layering enforced by `.importlinter`: `crr.core` must not import `crr.adapters`/`crr.cli`.
- No new command, no config key, no payload/contract version bump. `rescued_sessions`'s signature change is internal — `crr.cli` (`_cmd_rescued`, `_rescue_check`) is the only caller.
- Null results stay null (F16 tri-state): an unknown `list_sessions()` OR unknown `attached_sessions()` (either returns `None`) degrades to offering **nothing** — never a false "restored" claim, never a prompt.
- SAFETY: no test may touch real adapters (this machine runs production crr with live sessions), register a task, attach real tmux, or reboot. Tests drive injected fakes / monkeypatches only.
- Run the full suite with `.venv/bin/python -m pytest -q` and `.venv/bin/lint-imports` before each commit.

---

### Task 1: Reselect on parked-and-unattached (core + cli readers)

**Files:**
- Modify: `crr/core/rescue.py` (`rescued_sessions` signature + body + module docstring)
- Modify: `crr/cli.py` (`_cmd_rescued`, `_rescue_check` — resolve the attached set and call the new signature; the `[Y]` action stays `ops.detmux` for now)
- Test: `tests/test_rescue.py` (replace the boot-id selection test), `tests/test_cli.py` (add `attached_sessions()` to the two fake tmux classes; existing rescue tests keep passing)

**Interfaces:**
- Consumes: `tmux.attached_sessions() -> set[str] | None` (adapter method added in #32; `RealTmux` and the `TmuxSpawner` port already have it).
- Produces: `rescue.rescued_sessions(entries, live_tmux: set[str], attached_tmux: set[str]) -> list[dict]` — entries with a claude, whose `tmux_session` is in `live_tmux` and not in `attached_tmux`, sorted by pid.

- [ ] **Step 1: Rewrite the core selection test**

Replace `test_rescued_sessions_selects_prior_boot_tmux_parked_only` in `tests/test_rescue.py` with:

```python
def test_rescued_sessions_selects_parked_and_unattached():
    """A candidate is a conversation the reviver parked in a LIVE tmux
    session that the user has not opened yet (no client attached).

    boot_id is deliberately NOT consulted: the reviver re-keys a restored
    entry onto the live pane and stamps the CURRENT boot (#58), so keying
    on a boot mismatch found nothing after a real reboot (verified: `crr
    rescued` returned empty while 7 conversations sat parked).
    """
    e_ok       = _entry(pid=2, boot="cur", claude=True, tmux="crr-aaaaaaaa")  # parked, unopened
    e_attached = _entry(pid=3, boot="cur", claude=True, tmux="crr-bbbbbbbb")  # parked but opened
    e_noclaude = _entry(pid=4, boot="old", claude=False, tmux="crr-cccccccc")
    e_notmux   = _entry(pid=5, boot="old", claude=True, tmux=None)
    e_deadtmux = _entry(pid=6, boot="old", claude=True, tmux="crr-dddddddd")  # not live
    out = rescue.rescued_sessions(
        [e_deadtmux, e_ok, e_attached, e_noclaude, e_notmux],
        live_tmux={"crr-aaaaaaaa", "crr-bbbbbbbb"},
        attached_tmux={"crr-bbbbbbbb"},
    )
    assert [e["pid"] for e in out] == [2]


def test_rescued_sessions_empty_attached_offers_all_parked_sorted_by_pid():
    e_hi = _entry(pid=9, boot="cur", claude=True, tmux="crr-aaaaaaaa")
    e_lo = _entry(pid=1, boot="cur", claude=True, tmux="crr-bbbbbbbb")
    out = rescue.rescued_sessions(
        [e_hi, e_lo],
        live_tmux={"crr-aaaaaaaa", "crr-bbbbbbbb"},
        attached_tmux=set(),
    )
    assert [e["pid"] for e in out] == [1, 9]
```

(The `_entry` helper already accepts `boot=`; leave it — the field stays on entries, it's just no longer part of the predicate.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_rescue.py -q`
Expected: FAIL — old `rescued_sessions` takes `current_boot=` and rejects the new keyword args.

- [ ] **Step 3: Rewrite `rescued_sessions` in `crr/core/rescue.py`**

Replace the function with:

```python
def rescued_sessions(
    entries: Iterable[Mapping[str, Any]],
    live_tmux: set[str],
    attached_tmux: set[str],
) -> list[dict]:
    """Conversations the reviver restored and the user has NOT opened yet:
    parked in a live tmux session (``tmux_session in live_tmux``) with no
    client attached (``tmux_session not in attached_tmux``).

    boot_id is intentionally not a factor. The reviver re-keys a restored
    entry onto its live tmux pane and stamps the current boot (#58), so an
    earlier ``boot_id != current_boot`` predicate excluded exactly the
    conversations this prompt exists to offer. "Not attached" — the same
    signal the dashboard's ``attached`` badge uses (#32) — is what actually
    distinguishes a restored-but-unopened conversation from one the user is
    already sitting in.
    """
    out = [
        dict(e) for e in entries
        if e.get("claude") is not None
        and e.get("tmux_session")
        and e["tmux_session"] in live_tmux
        and e["tmux_session"] not in attached_tmux
    ]
    return sorted(out, key=lambda e: e["pid"])
```

Also update the module docstring's first paragraph (lines 3-6) to:

```python
"""Rescued-session selection + the per-boot restore-prompt marker.

A "rescued" session is a conversation the reviver parked in a live tmux
session that the user has not opened in a tab yet — parked-and-unattached
(#32's attached signal). The restore prompt (Phase 3 UX) offers exactly
that set once per boot.
```

- [ ] **Step 4: Run the core tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_rescue.py -q`
Expected: PASS.

- [ ] **Step 5: Give the two fake tmux classes an `attached_sessions()` method**

In `tests/test_cli.py`, `_FakeTmuxRescued` (nothing attached) and `_FakeTmuxUnknown` (unknown attached mirrors unknown live):

```python
class _FakeTmuxRescued:
    def __init__(self, *a, **k):
        pass

    def available(self):
        return True

    def list_sessions(self):
        return {"crr-8a1b2c3d"}

    def attached_sessions(self):
        return set()  # nothing opened yet -> the parked session is offered
```

```python
class _FakeTmuxUnknown:
    """F16: available() but list_sessions() can't determine liveness."""

    def __init__(self, *a, **k):
        pass

    def available(self):
        return True

    def list_sessions(self):
        return None

    def attached_sessions(self):
        return None
```

- [ ] **Step 6: Update the two cli readers in `crr/cli.py`**

In `_cmd_rescued`, replace the `live = ...` / `if live is None:` block and the `rescued_sessions(...)` call with:

```python
    live = tmux_spawner.list_sessions() if tmux_spawner.available() else set()
    attached = tmux_spawner.attached_sessions() if tmux_spawner.available() else set()
    if live is None or attached is None:
        # F16 tri-state: an unconfirmed live OR attached state must never be
        # read as "definitely rescued" — degrade to the same "no rescued
        # sessions" an unavailable tmux produces, never a guess. Say so on
        # stderr (mirrors the sibling journal-problems pattern below).
        print(
            "crr rescued: tmux state unknown — rescued sessions may be undercounted",
            file=sys.stderr,
        )
        live, attached = set(), set()
    store = JournalStore(state_dir.state_dir())
    scan = store.scan()
    found = rescue.rescued_sessions(scan.entries, live, attached)
```

In `_rescue_check`, replace the corresponding `live = ...` / `if live is None:` block and the `rescued_sessions(...)` call with:

```python
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    live = tmux_spawner.list_sessions() if tmux_spawner.available() else set()
    attached = tmux_spawner.attached_sessions() if tmux_spawner.available() else set()
    if live is None or attached is None:
        # F16 tri-state: never prompt on an unconfirmed tmux state. Same
        # stderr note as `crr rescued`; the interactive shims redirect this
        # command's stderr to /dev/null on shell startup, so it stays quiet
        # there — a manual `crr rescue-check` still sees it.
        print(
            "crr rescue-check: tmux state unknown — rescued sessions may be undercounted",
            file=sys.stderr,
        )
        live, attached = set(), set()
    store = JournalStore(sd)
    found = rescue.rescued_sessions(store.scan().entries, live, attached)
```

- [ ] **Step 7: Run the full suite + lint to verify green**

Run: `.venv/bin/python -m pytest -q && .venv/bin/lint-imports`
Expected: PASS, contract KEPT. (`test_rescued_lists_prior_boot_parked_sessions`, `test_rescued_reports_none*`, and the rescue-check tri-state tests still pass under the new selection — the parked entry is unattached, so it's still offered; the None-liveness cases still degrade to silence.)

- [ ] **Step 8: Commit**

```bash
git add crr/core/rescue.py crr/cli.py tests/test_rescue.py tests/test_cli.py
git commit -m "fix(core): rescue prompt selects parked-and-unattached, not boot mismatch (#30)"
```

---

### Task 2: The [Y] path reopens (keep tracked) instead of untracking

**Files:**
- Modify: `crr/cli.py` (`_rescue_check` — the `[Y]` action + prompt/notice wording)
- Test: `tests/test_cli.py` (update the two tests that monkeypatch `ops.detmux`)

**Interfaces:**
- Consumes: `ops.reopen(store, archive, tmux, controller, flags, boot, probe, pid, now, *, grace, remote_control, tab_spawner, tabs_expected) -> OpResult` — on a parked entry it attaches a tab to the tmux session and keeps the entry journaled (the dashboard Reopen op). Assembled exactly as `_cmd_reopen` does (`crr/cli.py:2649`).
- Produces: nothing new; behavior change only.

- [ ] **Step 1: Update the Y-path test to assert reopen (keep tracked)**

Replace `test_rescue_check_yes_opens_tabs_and_marks` in `tests/test_cli.py` with:

```python
def test_rescue_check_yes_reopens_tabs_keeping_them_tracked_and_marks(tmp_path, monkeypatch, capsys):
    # [Y] must REOPEN each restored conversation (attach a tab, keep it
    # tracked) — NOT detmux, which untracks. So the conversations stay on
    # the dashboard and survive the next reboot too (#30 / #33 principle).
    _rescue_check_setup(monkeypatch, tmp_path, [{"pid": 42}, {"pid": 43}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _FakeTab:
        def available(self):
            return True

    monkeypatch.setattr(cli, "_tab_spawner", lambda config: (_FakeTab(), True))
    monkeypatch.setattr(cli.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    calls = []

    def fake_reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                    pid, now, *, grace, remote_control, tab_spawner, tabs_expected):
        calls.append(pid)
        return SimpleNamespace(ok=True, degraded=False, message=f"reopened {pid} as crr-x")

    # If the old detmux path is still wired, this stays untouched and the
    # assertion on `calls` fails — the discriminator between the two ops.
    monkeypatch.setattr(cli.ops, "reopen", fake_reopen)
    monkeypatch.setattr(cli.ops, "detmux",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must reopen, not detmux")))

    rc = cli.main(["rescue-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [42, 43]
    assert "reopened 42" in out and "reopened 43" in out
    assert cli.rescue.already_prompted(tmp_path, "current-boot") is True
```

Also update `test_rescue_check_silent_when_marker_exists`: change the `ops.detmux` monkeypatch to `ops.reopen`:

```python
    monkeypatch.setattr(cli.ops, "reopen", lambda *a, **k: calls.append(a))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py -k rescue_check -q`
Expected: FAIL — the code still calls `ops.detmux`, so `fake_reopen` is never hit (`calls == []`) and the detmux stub raises.

- [ ] **Step 3: Rewrite the `[Y]` branch and wording in `_rescue_check`**

Replace the headless-notice line, the prompt line, and the `answer in ("", "y")` block. New wording + reopen loop:

```python
    n = len(found)
    tab, tabs_expected = _tab_spawner(config)
    if tab is None or not tab.available():
        print(f"crr: {n} conversation(s) restored after the last reboot — "
              "'crr rescued' lists them; attach with: tmux attach -t <name>")
        return 0

    print(f"crr: {n} conversation(s) restored after the last reboot. "
          "Open them in terminal tabs? [Y/n] ", end="", flush=True)
    timeout = config.get("rescue_prompt_timeout_seconds")
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        line = sys.stdin.readline() if ready else ""
    except KeyboardInterrupt:
        print()
        print("not now — 'crr rescued' lists them")
        return 0
    if not line:
        print()
    answer = line.strip().lower() if line else None

    if answer in ("", "y"):
        probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
        controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
        flags = FlagStore(sd)
        with mutation_lock(sd):
            for e in found:
                # reopen (NOT detmux): attach a tab AND keep the conversation
                # tracked, so it stays on the dashboard and is rescued again
                # after the next reboot (#30). Same op as the dashboard Reopen.
                res = ops.reopen(
                    JournalStore(sd), ArchiveStore(sd), tmux_spawner, controller, flags,
                    boot, probe, e["pid"], _now(),
                    grace=config.get("close_grace_seconds"),
                    remote_control=config.get("remote_control"),
                    tab_spawner=tab, tabs_expected=tabs_expected,
                )
                # The shims run `crr rescue-check 2>/dev/null`; the user typed
                # Y, so both outcomes go to stdout unconditionally.
                print(res.message)
    else:  # 'n'/'N', any other input, timeout, or EOF -> decline
        print("not now — 'crr rescued' lists them")
    return 0
```

Note: today this block opens with `tab, _tabs_expected = _tab_spawner(config)` (the `tabs_expected` value was thrown away because the old detmux path didn't need it). The replacement above binds it as `tab, tabs_expected = _tab_spawner(config)` and passes that `tabs_expected` to `ops.reopen`. `_tab_spawner` is resolved exactly once here (it already was — this block is the only place it's called in `_rescue_check`); do not add a second call.

- [ ] **Step 4: Run the rescue-check tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli.py -k rescue_check -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/lint-imports`
Expected: PASS (1833+), contract KEPT. `test_rescue_check_headless_prints_notice_once` asserts the substrings "restored after the last reboot", "'crr rescued' lists them", "tmux attach -t <name>" — the new wording keeps all three.

- [ ] **Step 6: Commit**

```bash
git add crr/cli.py tests/test_cli.py
git commit -m "feat(cli): restore prompt reopens (keeps tracked) instead of untracking (#30)"
```

---

## Self-Review

**Spec coverage:**
- Selection fix (spec §1) → Task 1 (`rescued_sessions` new signature/body).
- Both readers resolve attached (spec §3) → Task 1 Step 6.
- Action fix reopen-not-detmux (spec §2) → Task 2.
- Wording (spec §4) → Task 2 Step 3.
- Tri-state honesty (spec §3) → Task 1 (`if live is None or attached is None`) + the existing `_FakeTmuxUnknown` tests, extended with `attached_sessions()`.
- Once-per-boot / marker / tty / headless / recovery unchanged → no task touches `claim_prompt`/`already_prompted`; the marker tests are untouched.
- No new command/config/contract → confirmed; only `rescue.py` + `cli.py` + their tests change.

**Placeholder scan:** none — every step has concrete code.

**Type consistency:** `rescued_sessions(entries, live_tmux, attached_tmux)` is used identically in Task 1 Step 3 (def), Task 1 Step 6 (both call sites), and the tests. `ops.reopen(...)`'s keyword args in Task 2 Step 1 (fake) and Step 3 (call) match `_cmd_reopen`'s real call (`grace`, `remote_control`, `tab_spawner`, `tabs_expected`). `attached_sessions()` matches the adapter method added in #32.
