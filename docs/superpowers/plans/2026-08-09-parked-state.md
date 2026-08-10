# Parked State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the dashboard reporting a session as `crashed` when it is
running fine inside a live tmux session.

**Architecture:** `classify()` is the *operational* classifier — it answers
"may I act on this pid", and `crashed` is the correct answer for a parked
session (`ops.detmux`/`ops.untmux` guard on exactly that). It is NOT
touched. The card's `state` is a *display projection*, computed in
`crr/core/status.py` alone: when the operational state is `crashed` but the
entry's `tmux_session` is confirmed alive, the card reads `parked`. The
tmux-liveness set is injected into `assemble_sessions` once per poll, the
same shape as `tail_facts`, so core does no I/O.

**Tech Stack:** Python 3.12 stdlib only. pytest. import-linter.

**Spec:** `docs/superpowers/specs/2026-08-09-reachability-design.md`, Phase 0.

## Global Constraints

- One-way layering `crr.cli → crr.adapters → crr.core`, enforced by
  `.venv/bin/lint-imports`, which must print `KEPT`. `crr.core` imports
  neither adapters nor cli.
- Runtime dependencies stay at zero (stdlib only).
- TDD: write the test, watch it fail, then implement. A test that passes
  on first run proves nothing.
- Contract shapes are versioned. A changed served shape bumps its version
  constant, and `tests/test_version_ledger.py` fails unless the bump has a
  ledger comment entry.
- A pre-commit hook runs the full suite + lint on every commit; the tree
  must be green to commit.
- `crr/core/classifier.py`, `crr/core/ops.py`, `crr/core/reviver.py` and
  `crr/cli.py::_kick_dropped_bridges` MUST NOT be modified by this plan.
  If a task seems to need it, stop — the design is being violated.
- F16 tri-state: `tmux.list_sessions()` returns `set | None`. `None` means
  "could not determine" and may never be treated as "no sessions exist".
- **Tasks 1 and 2 are NOT independently shippable and must never be merged
  apart.** Task 1 teaches `status.py` to emit `parked`; Task 2 teaches
  `contracts.STATES` to accept it. Between them the assembler can produce a
  card its own validator rejects. The suite stays green only because no
  test both passes `live_tmux_sessions` and calls
  `validate_sessions_payload` — a gap, not a guarantee. (Found by the Task 1
  implementer, 2026-08-10.)
- When a step says "if the test passes here, stop", that applies only to the
  NEW tests named in that step. A `-k` selector may also match pre-existing
  tests, which pass unconditionally and are not a signal.
- **Version floors verified against `main` at 7b0f9c6:** `PAGE_VERSION`
  is 43, `SESSIONS_CONTRACT_VERSION` is 10, `CONFIG_DEFAULTS_VERSION` is
  14. Re-check these before starting — another session shipped #49 while
  this plan was being written and took v43, and a stale bump fails
  `tests/test_version_ledger.py`.

---

### Task 1: The pure display projection

**Files:**
- Modify: `crr/core/status.py`
- Test: `tests/test_status.py`

**Interfaces:**
- Consumes: `crr.core.classifier.classify`, `CRASHED` (existing).
- Produces: `crr.core.status.PARKED = "parked"`, and
  `assemble_sessions(..., live_tmux_sessions: set[str] | None = None)` —
  later tasks pass the real set in from `cli`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_status.py`:

```python
def test_a_crashed_entry_parked_in_a_live_tmux_session_reads_parked():
    # After a reboot the reviver restores conversations into detached tmux.
    # The journal keeps the pre-reboot pid and boot_id, so classify() says
    # CRASHED — correct for "may I act on this pid", wrong for the card.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-8a1b2c3d"})
    assert payload["sessions"][0]["state"] == "parked"


def test_a_crashed_entry_whose_tmux_session_is_gone_stays_crashed():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions=set())
    assert payload["sessions"][0]["state"] == "crashed"


def test_unknown_tmux_state_never_promotes_to_parked():
    # F16 tri-state: None means "could not determine". Promoting on it
    # would assert a session is running on the strength of a failed query.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions=None)
    assert payload["sessions"][0]["state"] == "crashed"


def test_an_entry_with_no_tmux_session_is_unaffected():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-other"})
    assert payload["sessions"][0]["state"] == "crashed"


def test_a_live_session_is_never_demoted_to_parked():
    # The projection is one-directional: tmux liveness may only rescue an
    # entry from a wrong `crashed`, never push one into parked.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-8a1b2c3d"})
    assert payload["sessions"][0]["state"] == "live"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_status.py -q -k "parked or tmux"`

Expected: FAIL — `assemble_sessions() got an unexpected keyword argument
'live_tmux_sessions'`, on all five NEW tests. The selector also matches the
pre-existing `test_card_carries_tmux_session`, which passes and is not a
signal. If one of the five new tests passes here, stop: the behaviour
already exists and the test is not testing what it claims.

- [ ] **Step 3: Implement the projection**

In `crr/core/status.py`, add the constant next to the existing imports:

```python
from crr.core.classifier import CRASHED, classify

# Display-only state (spec 2026-08-09, Phase 0). NOT a classifier state:
# `classify()` answers "may I act on this pid", and CRASHED is the right
# answer for a parked session — `ops.detmux`/`ops.untmux` guard on exactly
# that and re-home only crashed entries. This projection changes what the
# CARD says, and nothing else.
PARKED = "parked"
```

Add the parameter to `assemble_sessions`:

```python
    live_tmux_sessions: set[str] | None = None,
```

with this docstring paragraph:

```
    ``live_tmux_sessions`` is the set of tmux session names confirmed
    alive, resolved ONCE per poll by the caller (core does no I/O).
    ``None`` is F16's honest "could not determine" and never promotes an
    entry — an unconfirmed query may not assert that a session is running.
```

Replace the card's `state` line:

```python
                "state": _display_state(entry, boot_identity, probe, live_tmux_sessions),
```

and add the helper above `assemble_sessions`:

```python
def _display_state(entry, boot_identity, probe, live_tmux_sessions) -> str:
    """The card's state: the operational state, except that a CRASHED entry
    parked in a confirmed-live tmux session reads PARKED.

    One-directional by construction: only CRASHED is ever rewritten, so
    tmux liveness can rescue an entry from a wrong `crashed` but can never
    push a live or ghost session into `parked`.
    """
    state = classify(entry, boot_identity, probe)
    if state != CRASHED or not live_tmux_sessions:
        return state
    name = entry.get("tmux_session")
    return PARKED if name and name in live_tmux_sessions else state
```

Note `not live_tmux_sessions` covers both `None` (unknown) and `set()`
(none alive) — neither may promote.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_status.py -q`

Expected: PASS, with no other test in the file broken (the new parameter
defaults to `None`, so every existing caller is unchanged).

- [ ] **Step 5: Commit**

```bash
git add crr/core/status.py tests/test_status.py
git commit -m "feat(status): a tmux-parked session reads 'parked', not 'crashed'"
```

---

### Task 2: Contract the new state

**Files:**
- Modify: `crr/core/contracts.py`
- Modify: `crr/core/status.py` — **one word**, the module docstring's
  `(contract v10)` → `(contract v11)`. This is FORCED, not optional:
  `tests/test_version_ledger.py::test_status_docstring_version_matches_the_shipped_contract`
  regexes that docstring out of `status.py`'s text and compares it to
  `SESSIONS_CONTRACT_VERSION`, so bumping the constant without it leaves
  the tree red and the pre-commit hook refuses the commit. No logic in
  `status.py` may change. (The first draft of this plan listed `status.py`
  as untouchable and was wrong — found by the Task 2 implementer.)
- Test: `tests/test_contracts.py`, `tests/test_version_ledger.py` (no edit,
  must keep passing)

**Interfaces:**
- Consumes: `status.PARKED` from Task 1.
- Produces: `contracts.STATES` including `"parked"`;
  `SESSIONS_CONTRACT_VERSION == 11`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contracts.py`:

```python
def test_states_enum_includes_parked():
    assert contracts.STATES == ("live", "ghost", "crashed", "parked")


def test_a_parked_card_validates():
    p = _sessions_payload()
    p["sessions"][0]["state"] = "parked"
    contracts.validate_sessions_payload(p)
```

And change the existing version test:

```python
def test_sessions_contract_version_is_11():
    # v11 adds the `parked` display state (spec 2026-08-09, Phase 0).
    assert contracts.SESSIONS_CONTRACT_VERSION == 11
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_contracts.py -q -k "parked or version_is_11"`

Expected: FAIL — `STATES == ("live","ghost","crashed")`, and `11 != 10`.

- [ ] **Step 3: Implement**

In `crr/core/contracts.py`, extend the enum:

```python
# "parked" (spec 2026-08-09, Phase 0) is a DISPLAY state only — a session
# the reviver restored into a live tmux session. `classify()` still calls
# it CRASHED, which is what `ops.detmux`/`ops.untmux` require; the card
# says what a reader needs instead of what the op guard needs.
STATES = ("live", "ghost", "crashed", "parked")
```

Add the ledger entry immediately above `SESSIONS_CONTRACT_VERSION` and
bump it:

```python
# v11 adds the `parked` display state (spec 2026-08-09, Phase 0) — a card
# whose session the reviver restored into a live tmux session
SESSIONS_CONTRACT_VERSION = 11
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_contracts.py tests/test_version_ledger.py -q`

Expected: PASS. The ledger guard fails if the `# v11` comment is missing,
so a bump without a reason cannot land.

- [ ] **Step 5: Commit**

```bash
git add crr/core/contracts.py tests/test_contracts.py
git commit -m "feat(contracts): sessions v11 adds the parked display state"
```

---

### Task 3: Wire the real tmux set in from the composition root

**Files:**
- Modify: `crr/cli.py` (the three `assemble_sessions(` call sites)
- Test: `tests/test_cli.py`, `tests/test_status.py`,
  `tests/test_e2e_linux.py` — the last one is NOT optional. It is the only
  test in the suite that stands up a real tmux server and actually revives
  a session, and its `assert card["state"] == "crashed"` predates `parked`.
  It will fail with `assert 'parked' == 'crashed'` once the wiring lands.
  That failure is the feature working end to end; update the assertion (do
  not weaken it) and say so in a comment.

**Interfaces:**
- Consumes: `assemble_sessions(live_tmux_sessions=…)` from Task 1.
- Produces: nothing new; `/api/sessions`, `crr status --json` and
  `crr whoami` all begin emitting `parked`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_status_json_reports_parked_for_a_tmux_restored_session(tmp_path, monkeypatch, capsys):
    # End to end through the composition root: a pre-reboot entry whose
    # tmux session is alive must not print as crashed.
    from crr.adapters import state_dir, tmux
    from crr.core.journal import JournalStore, new_entry

    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    JournalStore(tmp_path).write(new_entry(
        pid=999999, cwd="/home/u/p", host="tmux", shell="bash",
        boot_id="a-previous-boot", now="2026-01-01T00:00:00+00:00",
        tmux_session="crr-8a1b2c3d",
        claude={"session_id": sid, "sid_source": "injected",
                "started": "2026-01-01T00:00:00+00:00"}))

    class FakeTmux:
        def available(self): return True
        def list_sessions(self): return {"crr-8a1b2c3d"}

    monkeypatch.setattr(tmux, "RealTmux", lambda *a, **k: FakeTmux())
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["state"] == "parked"
```

Also add to `tests/test_status.py` — the assembler and the validator in ONE
path. Task 1 tested the assembler, Task 2 tested the validator, and between
them the assembler could emit a state the validator rejected with nothing to
catch it. That hole stayed open through both tasks (found by the Task 2
implementer); close it here:

```python
def test_a_parked_card_survives_its_own_validator():
    from crr.core import contracts
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    entry = _entry(42, sid)
    entry["boot_id"] = "an-old-boot"
    entry["tmux_session"] = "crr-8a1b2c3d"
    payload = assemble_sessions(
        [entry], FakeBoot(), FakeProbe(), live_tmux_sessions={"crr-8a1b2c3d"})
    assert payload["sessions"][0]["state"] == "parked"
    contracts.validate_sessions_payload(payload)   # the half nothing covered
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py tests/test_status.py -q -k parked`

Expected: the cli test FAILs with `assert 'crashed' == 'parked'`. The
status test PASSES already — Tasks 1 and 2 made it true; it is a
regression pin, not a driver, and that is why it belongs here rather than
being skipped.

- [ ] **Step 3: Implement**

Add this helper to `crr/cli.py`, next to `_tail_facts_extractor`:

```python
def _live_tmux_sessions(config: cfg.Config) -> set[str] | None:
    """Tmux session names confirmed alive, or None when tmux cannot say.

    Resolved ONCE per status build and injected into `assemble_sessions`
    (core does no I/O). Returns None on F16's tri-state unknown so the
    display projection declines to promote anything — an unconfirmed query
    must not assert that a session is running.
    """
    t = tmux.RealTmux(config.get("interop_timeout_seconds"))
    if not t.available():
        return set()
    return t.list_sessions()
```

Then at each of the three `assemble_sessions(` call sites in `cli.py`
(the `status` command, the web provider, and `_whoami_card` — which also
backs the `session-start` hook, so every Claude session start pays one
extra tmux fork; once per session, inside an existing catch-all, judged
acceptable), add:

```python
        live_tmux_sessions=_live_tmux_sessions(config),
```

Locate them with:

```bash
grep -n "assemble_sessions(" crr/cli.py
```

There are exactly three. Resolve the set INSIDE the web `provider()`
closure, not hoisted beside `_tail_facts_extractor` — hoisting freezes tmux
liveness at service startup and the dashboard answers with boot-time state
forever. All three must be updated — a missed one shows
`crashed` on that surface while the others show `parked`, which is the
same inconsistency this plan exists to remove.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q && .venv/bin/lint-imports`

Expected: PASS (1272 at time of writing) and `Contracts: 1 kept, 0 broken`.

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_cli.py tests/test_status.py tests/test_e2e_linux.py
git commit -m "feat(cli): inject live tmux sessions so parked cards render everywhere"
```

---

### Task 4: Render it on the dashboard

**Files:**
- Modify: `crr/core/page.html`, `crr/core/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: cards carrying `state: "parked"` from Task 3.
- Produces: `PAGE_VERSION == 44`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:

```python
def test_page_version_is_44():
    """v44: the parked state renders as 'restored' (spec 2026-08-09, Phase 0)
    (v43 added the amber NO TAB notice for a degraded reopen, #49)."""
    assert web.PAGE_VERSION == 44


def test_page_renders_the_parked_state():
    page = web.load_page()
    assert "k-parked" in page
    assert "restored" in page


def test_key_explains_the_parked_state():
    page = web.load_page()
    key = page[page.index('id="key"'):page.index('id="key"') + 4000]
    assert "restored" in key


def test_parked_cards_get_the_crashed_action_set():
    # ops.py classifies a parked session as CRASHED, so it accepts
    # Reopen/Dismiss/Untrack/Un-tmux and refuses Kick/Close. If the card
    # branches on "crashed" alone, a parked card offers two buttons that
    # always fail and loses the four that work.
    page = web.load_page()
    assert 's.state === "crashed" || s.state === "parked"' in page


def test_parked_renders_as_restored_not_as_the_raw_enum():
    page = web.load_page()
    assert 's.state === "parked" ? "restored" : s.state' in page
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_web.py -q -k "parked or version_is_44"`

Expected: FAIL — `44 != 43`, and `"k-parked" not in page`.

- [ ] **Step 3: Implement**

In `crr/core/page.html`, add the state colour beside the existing
`.k-live` / `.k-ghost` / `.k-crashed` rules:

```css
  .k-parked::before { background: #46c2b0; }
```

Add a key term inside the `state` group, after the `crashed` term:

```html
<span class="k-parked kterm" data-help="Running inside a detached tmux session — crr restored it after a reboot. The conversation is alive; Reopen brings it back into a terminal tab, or attach with: tmux attach -t &lt;name&gt;.">restored</span>
```

Add the badge colour beside `.badge.crashed` (teal, matching `k-parked` —
it is a healthy state, not an alarm):

```css
  .badge.parked { background: #123a3a; color: #46c2b0; }
```

The chip currently prints the raw enum:

```js
  var badge = el("span", "badge " + s.state);
  badge.textContent = s.state;
```

Change the text so the card says `restored` while the class stays keyed to
the contract value:

```js
  var badge = el("span", "badge " + s.state);
  // "parked" is the contract value; "restored" is what a reader needs.
  badge.textContent = s.state === "parked" ? "restored" : s.state;
```

**The action set must follow the operational state, not the display one.**
`ops.py` still classifies a parked session as CRASHED, so it accepts
Reopen / Dismiss / Untrack / Un-tmux and REFUSES Kick / Close
("session N is crashed, not running"). The card branches on
`s.state === "crashed"`, so without this change a parked card falls into
the `else` branch, offers Kick and Close that always fail, and loses the
four buttons it actually needs. Change the branch:

```js
  // parked is CRASHED operationally (see spec Phase 0) — ops.py accepts
  // exactly these four and refuses Kick/Close, so the card must offer the
  // same set it does for crashed.
  if (s.state === "crashed" || s.state === "parked") {
```

Leave the `else` branch and its `s.state === "ghost"` check untouched.

**The filters** at the top of the same file read:

```js
  if (filterKey === "crashed") return s.state === "crashed";
  if (filterKey === "active") return s.state !== "crashed";
```

Leave both exactly as they are. A parked session is genuinely running, so
it belongs in `active` and not in `crashed` — that falls out correctly
without an edit. Note the visible consequence and do not "fix" it: after a
reboot the `crashed` filter will show far fewer sessions than before,
because most of them are now honestly reported as restored.

In `crr/core/web.py`:

```python
PAGE_VERSION = 44  # v44: the parked state renders as "restored" (Phase 0)
```

- [ ] **Step 4: Run the tests and the JS gate**

Run: `.venv/bin/python -m pytest -q`

Then verify the served page still parses — a raw `@PLACEHOLDER@` or a
syntax error renders a blank dashboard:

```bash
.venv/bin/python - <<'EOF'
import re, subprocess, tempfile
from crr.core import web
for i, b in enumerate(re.findall(r"<script[^>]*>(.*?)</script>", web.render_page(1), re.S)):
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False); f.write(b); f.close()
    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    print(i, "OK" if r.returncode == 0 else "FAIL " + r.stderr[:300])
EOF
```

Expected: all tests PASS and every block prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py
git commit -m "feat(web): render the parked state as 'restored' (page v44)"
```

---

### Task 5: Merge, deploy, verify on the real machine

**Files:** none (verification only).

- [ ] **Step 0: Confirm Tasks 1 and 2 are both present**

```bash
git log --oneline main..HEAD
```

Expected: at least the Task 1 and Task 2 commits. Merging Task 1 without
Task 2 ships an assembler that can emit a card its own validator rejects.

- [ ] **Step 1: Confirm the untouchable files were not touched**

```bash
git diff main --stat -- crr/core/classifier.py crr/core/ops.py crr/core/reviver.py
```

Expected: **empty output.** Any change here means the design was violated
— `detmux`/`untmux`/`dismiss` guards must still see `CRASHED`.

- [ ] **Step 2: Confirm the destructive-op guards still behave**

```bash
.venv/bin/python -m pytest tests/test_ops.py -q
```

Expected: PASS, unchanged. These cover the `!= CRASHED` refusals this plan
deliberately preserves.

- [ ] **Step 3: Merge on local green**

```bash
git fetch -q origin && git rev-parse origin/main   # confirm unmoved
git checkout main
git merge --no-ff -m "Merge phase 0: a restored session stops reading crashed" worktree-phase0-parked-state
.venv/bin/python -m pytest -q && .venv/bin/lint-imports
```

- [ ] **Step 4: Deploy and verify against the live machine**

```bash
systemctl --user restart crr-web.service && sleep 2
curl -s http://127.0.0.1:8377/api/version
curl -s http://127.0.0.1:8377/api/sessions | python3 -c "
import json,sys,collections
d=json.load(sys.stdin)
print('contract', d['contract'], dict(collections.Counter(s['state'] for s in d['sessions'])))"
```

Expected: `{"version": 44}`, `contract 11`, and the state histogram now
showing `parked` for the sessions `tmux ls` lists as `crr-*`. Before this
change the machine reported `{'crashed': 16, 'live': 1}` while 14 claude
processes were running — that count is the regression being fixed.

- [ ] **Step 5: Cross-check the two subsystems now agree**

```bash
.venv/bin/crr revive 2>&1 | tail -1     # "revived 0, gave up 0, already running N"
.venv/bin/crr rescued | wc -l
```

Expected: the "already running N" count and the number of `parked` cards
describe the same sessions. The whole point of this plan is that
`crr revive` and the dashboard stop contradicting each other.

- [ ] **Step 6: Push**

```bash
git push origin main
```

---

## Remaining plans

This plan covers Phase 0 only. Still to be written, per the spec's
Sequencing section:

- **Plan 2 — Phases 1–3:** the `bridgeSessionId` detector, the
  `status`-driven kick rule, and the reachability card. One unit; the
  detector is useless without the card and the card cannot render states
  the detector does not produce.
- **Plan 3 — Phase 4:** the dashboard restore banner, removing
  `rescue-check` from the shims (requires shim regeneration — release
  note).
- **Plan 4 — Phase 5:** the observed-transition counter surfaced by
  `crr doctor`.
