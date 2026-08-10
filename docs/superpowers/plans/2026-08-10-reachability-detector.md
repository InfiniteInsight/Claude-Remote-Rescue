# Reachability Detector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the record-counting dropped-bridge detector with Claude Code's own `bridgeSessionId` state, so an idle disconnected session is detected within one 30s sweep instead of never.

**Architecture:** A new adapter reads `~/.claude/sessions/*.json` (Claude Code's own per-process state) once per poll and returns a `sid -> SessionState` map. A new pure-core module classifies each into `reachable` / `unreachable` / `unknown` and decides whether a `status` value permits a kick. `cli` builds the map and injects it, exactly as it already does for `tail_facts` and `live_tmux_sessions`. The transcript-marker counting is deleted.

**Tech Stack:** Python 3.12 stdlib only. pytest. import-linter.

**Spec:** `docs/superpowers/specs/2026-08-09-reachability-design.md`, Phases 1–3.

## Global Constraints

- One-way layering `crr.cli → crr.adapters → crr.core`, enforced by
  `.venv/bin/lint-imports`, which must print `KEPT`. `crr.core` imports
  neither adapters nor cli.
- Runtime dependencies stay at zero (stdlib only).
- TDD: write the test, RUN it, watch it fail for the right reason, then
  implement. A new test that passes before implementation is a broken test
  — report it rather than proceeding.
- A pre-commit hook runs the full suite + `lint-imports` (~60s). Green tree
  required to commit.
- `git add <explicit paths>` only. Never `git add -A`.
- **Version floors verified against `main` at `ae5f7af`:** `PAGE_VERSION`
  45, `SESSIONS_CONTRACT_VERSION` 11, `CONFIG_DEFAULTS_VERSION` 14.
  **Re-check all three before bumping.** Another agent ships to this repo
  daily and has taken a page version out from under this work twice.
  `tests/test_page_version_guard.py` now fails on a page change without a
  bump, and `tests/test_version_ledger.py` fails on a bump without a ledger
  comment — both are deliberate, do not weaken either.
- **Never touch** `/home/evan/projects/Claude-Remote-Rescue` (the shared
  checkout). All work happens in the worktree.
- Test with `.venv/bin/pytest -q`. Both `.venv/bin/pytest` and
  `.venv/bin/python` are worktree-local shims; **do not modify anything
  under `.venv/`** — the real console script imports the wrong source tree.

## Honest limits, carried from the spec

Two things this design does NOT fix. Do not paper over them in comments or
tests:

1. **Error-state losses are invisible.** Claude Code never persists
   `replBridgeError` — all 8 writers to the state file were enumerated. A
   bridge that comes up and then errors *without* teardown leaves a stale
   session id, and the detector reads `reachable`. The miss is silent but
   safe (no kick, never a wrong kick). Task 7 makes it countable.
2. **`~/.claude/sessions/*.json` is undocumented internal state.** Every
   read degrades to `unknown`, never to a positive claim.

**Interface note for Tasks 3-6, established in Task 2.** `field_present`
means "there is a READABLE answer", not merely "the key exists". A
`bridgeSessionId` that is neither a string nor null — a future Claude Code
reshaping it to `{"id": ...}` — reports `field_present=False`. The plan's
first draft returned `field_present=True, bridge_session_id=None` for that
case, which `reachability()` classifies **`unreachable`** and Task 4
**kicks**: a live process restarted on the strength of a value crr could
not parse. Caught and fixed during Task 2; verified by construction.

**Real-machine scale, measured in Task 2:** 143 files, **70** unique
session ids — 21 reachable, 24 unreachable, 25 field-absent. `read_all`
applies no liveness filter by design; `pid_matched` is the caller's job.
One session id had **nineteen** state files, and 23 of 70 had more than
one, so newest-wins carries more weight than the plan first implied.

---

### Task 1: The pure classifier

**Files:**
- Create: `crr/core/reachability.py`
- Create: `tests/test_reachability.py`

**Interfaces:**
- Produces: `reachability(bridge_session_id, *, pid_matched, field_present) -> str`
  returning one of `"reachable" | "unreachable" | "unknown"`; and
  `may_kick(status) -> tuple[bool, str]` returning `(allowed, reason)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Reachability classification (spec 2026-08-09, Phases 1-2).

Pure core: decides whether a session's phone link is up, and whether its
reported activity permits restarting it. No I/O — the adapter samples
Claude Code's own per-process state file, this module only judges. Mirrors
`crr.core.takeover.ready_to_take_over`'s shape.
"""

import pytest

from crr.core import reachability as r


# --- reachability() -------------------------------------------------------

def test_a_live_bridge_session_id_is_reachable():
    assert r.reachability("session_013C", pid_matched=True, field_present=True) == "reachable"


def test_a_null_bridge_session_id_is_unreachable():
    assert r.reachability(None, pid_matched=True, field_present=True) == "unreachable"


def test_an_empty_string_is_unreachable_not_reachable():
    # Falsy but present: Claude Code writes null, but a "" would otherwise
    # sneak through a truthiness check as a live session id.
    assert r.reachability("", pid_matched=True, field_present=True) == "unreachable"


def test_a_pid_that_does_not_match_is_unknown():
    # 117 of 133 state files on the author's machine belong to dead pids,
    # and 2 to RECYCLED pids now owned by unrelated processes. One session
    # had three files, two with "alive" pids. Liveness alone lies.
    assert r.reachability("session_013C", pid_matched=False, field_present=True) == "unknown"


def test_a_missing_bridge_field_is_unknown_not_unreachable():
    # An older Claude Code, or a renamed field. Absence of the field is not
    # evidence the bridge is down.
    assert r.reachability(None, pid_matched=True, field_present=False) == "unknown"


def test_pid_mismatch_wins_over_everything():
    assert r.reachability(None, pid_matched=False, field_present=False) == "unknown"


# --- may_kick() -----------------------------------------------------------

@pytest.mark.parametrize("status", ["busy", "shell"])
def test_work_in_flight_is_never_kicked(status):
    allowed, reason = r.may_kick(status)
    assert allowed is False
    assert status in reason


def test_idle_may_be_kicked():
    allowed, _ = r.may_kick("idle")
    assert allowed is True


def test_waiting_may_be_kicked():
    # The deadlock-breaker. A session blocked on a permission prompt never
    # reaches a clean assistant-end turn boundary, so a boundary-only guard
    # would refuse forever exactly the session that most needs unsticking —
    # blocked on a question the user cannot answer, because the phone is
    # disconnected.
    allowed, _ = r.may_kick("waiting")
    assert allowed is True


@pytest.mark.parametrize("status", [None, "", "some-future-status"])
def test_an_unrecognised_status_is_never_kicked(status):
    allowed, reason = r.may_kick(status)
    assert allowed is False
    assert "unknown" in reason.lower() or "unrecognised" in reason.lower()


def test_idle_and_waiting_are_the_only_permitted_statuses():
    permitted = {s for s in ("idle", "busy", "shell", "waiting") if r.may_kick(s)[0]}
    assert permitted == {"idle", "waiting"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_reachability.py -q`

Expected: collection error, `ImportError: cannot import name 'reachability' from 'crr.core'`. (NOT `ModuleNotFoundError` — `crr.core` is an existing package, so CPython's `_handle_fromlist` swallows that and `IMPORT_FROM` raises plain `ImportError`.)

- [ ] **Step 3: Implement**

Create `crr/core/reachability.py`:

```python
"""Is this session reachable from the phone, and may it be restarted?
(spec 2026-08-09, Phases 1-2 — replaces the record-counting detector.)

Pure core: two predicates over facts an adapter already sampled from
Claude Code's own per-process state file. No I/O, no clock — mirrors
``crr.core.takeover.ready_to_take_over``'s shape.

Why this replaced counting transcript records: the old detector needed a
median of 8 minutes of ACTIVE work to fire (measured across 93 transcripts
/ 3,659 continuously-active windows) and never fired at all on an idle
session, because an idle session writes no records. That is precisely the
session the feature exists for — the one sitting disconnected while its
owner is away from the keyboard. The old design treated it as a false
positive; it is the case.
"""

from __future__ import annotations

REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

# Claude Code's own `status` values, observed on disk. `waiting` carries a
# `waitingFor` describing what it is blocked on ("permission prompt",
# "input needed").
_KICKABLE = ("idle", "waiting")
_BUSY = ("busy", "shell")


def reachability(
    bridge_session_id: str | None, *, pid_matched: bool, field_present: bool,
) -> str:
    """Classify the phone link as reachable / unreachable / unknown.

    Every failure route lands on ``unknown``; none may produce a positive
    claim. In order:

    - ``pid_matched`` False — the newest state file for this session id
      belongs to a pid that is NOT one of this session's live claude
      processes. It is a leftover from a dead process, or worse a RECYCLED
      pid now owned by something unrelated. Measured on the author's
      machine: 117 of 133 state files had dead pids and 2 had recycled
      ones, and one session had three files with two "alive" pids. A
      liveness check alone returns a confident wrong answer.
    - ``field_present`` False — the file carries no ``bridgeSessionId`` key
      at all (an older Claude Code, or a renamed field). Absence of the
      field is not evidence the bridge is down.
    - otherwise the id itself decides. Falsy (``None``, and ``""`` for
      safety) is ``unreachable``; anything else is ``reachable``.
    """
    if not pid_matched or not field_present:
        return UNKNOWN
    return REACHABLE if bridge_session_id else UNREACHABLE


def may_kick(status: str | None) -> tuple[bool, str]:
    """Does this session's reported activity permit restarting it?

    ``(True, "")`` when it does; ``(False, <human reason>)`` when it does
    not. Kicking destroys whatever turn is in flight, so the two working
    states are hard blocks:

    - ``busy``    — claude is generating. This is where work dies.
    - ``shell``   — a command is running under it.
    - ``idle``    — a completed turn, nothing in flight. Safe.
    - ``waiting`` — blocked on the USER (a permission prompt, or a question).
      Safe, and the important one: such a session never reaches a clean
      assistant-end turn boundary, so a boundary-only guard would refuse it
      forever — leaving it stuck on a question its owner cannot answer,
      because the phone is disconnected. Restarting loses at most one
      pending tool call; the conversation resumes intact.

    Anything unrecognised — including ``None`` and a status a future Claude
    Code invents — is refused. An unreadable signal is not a licence to
    signal a live process.
    """
    if status in _KICKABLE:
        return True, ""
    if status in _BUSY:
        return False, f"session is {status} — work in flight"
    return False, f"unknown activity status {status!r} — refusing to kick"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_reachability.py -q && .venv/bin/lint-imports`

Expected: PASS, and `Contracts: 1 kept, 0 broken`.

- [ ] **Step 5: Commit**

```bash
git add crr/core/reachability.py tests/test_reachability.py
git commit -m "feat(core): reachability + may_kick, the detector's pure half"
```

---

### Task 2: The adapter that reads Claude Code's state files

**Files:**
- Create: `crr/adapters/session_state.py`
- Create: `tests/test_session_state.py`

**Interfaces:**
- Consumes: nothing from Task 1 (deliberately independent — the adapter
  samples, core judges).
- Produces: `SessionState` (a NamedTuple: `pid: int | None`,
  `bridge_session_id: str | None`, `field_present: bool`, `status: str | None`,
  `waiting_for: str`), and `read_all(home: Path | None = None) -> dict[str, SessionState]`
  keyed by session id.

- [ ] **Step 1: Write the failing tests**

```python
"""Claude Code's own per-process state files (spec 2026-08-09, Phase 1).

`~/.claude/sessions/<pid>.json` is written by Claude Code itself and carries
`bridgeSessionId` (null when the phone link is down), `status`, and
`waitingFor`. Undocumented internal state: every read degrades to a missing
entry or an honest `field_present=False`, never to a fabricated value.
"""

import json

from crr.adapters import session_state

SID_A = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SID_B = "1234abcd-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _write(home, pid, sid, **fields):
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "sessionId": sid}
    payload.update(fields)
    (d / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_reads_a_connected_session(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId="session_013C", status="idle")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].bridge_session_id == "session_013C"
    assert got[SID_A].field_present is True
    assert got[SID_A].status == "idle"
    assert got[SID_A].pid == 100


def test_a_null_bridge_is_read_as_none_with_the_field_present(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId=None, status="idle")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].bridge_session_id is None
    assert got[SID_A].field_present is True   # null is an ANSWER, not an absence


def test_a_file_without_the_field_reports_field_present_false(tmp_path):
    _write(tmp_path, 100, SID_A, status="idle")
    assert session_state.read_all(tmp_path)[SID_A].field_present is False


def test_waiting_for_is_carried(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId=None,
           status="waiting", waitingFor="permission prompt")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].status == "waiting"
    assert got[SID_A].waiting_for == "permission prompt"


def test_absent_waiting_for_is_an_empty_string_not_none(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId=None, status="idle")
    assert session_state.read_all(tmp_path)[SID_A].waiting_for == ""


@pytest.mark.parametrize("stale_pid,live_pid", [(100, 200), (200, 100)])
def test_the_newest_file_wins_when_a_session_has_several(tmp_path, stale_pid, live_pid):
    # Observed live: 23 of 70 session ids had more than one state file, one
    # of them NINETEEN. Only the newest describes the running process.
    #
    # Parametrised both directions deliberately: `Path.glob` yields
    # filesystem order, so a single-direction test passes against a
    # first-globbed-wins implementation as readily as a correct one. Running
    # it both ways leaves mtime as the only rule that satisfies both.
    import os, time
    _write(tmp_path, 100, SID_A, bridgeSessionId=None, status="idle")
    _write(tmp_path, 200, SID_A, bridgeSessionId="session_new", status="busy")
    old = tmp_path / ".claude" / "sessions" / "100.json"
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    got = session_state.read_all(tmp_path)
    assert got[SID_A].pid == 200
    assert got[SID_A].bridge_session_id == "session_new"


def test_separate_sessions_are_kept_separate(tmp_path):
    _write(tmp_path, 100, SID_A, bridgeSessionId="session_a", status="idle")
    _write(tmp_path, 200, SID_B, bridgeSessionId=None, status="idle")
    got = session_state.read_all(tmp_path)
    assert got[SID_A].bridge_session_id == "session_a"
    assert got[SID_B].bridge_session_id is None


def test_a_corrupt_file_is_skipped_without_raising(tmp_path):
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path, 100, SID_A, bridgeSessionId="session_013C", status="idle")
    got = session_state.read_all(tmp_path)
    assert SID_A in got            # the good file still read
    assert len(got) == 1


def test_a_file_without_a_session_id_is_skipped(tmp_path):
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    (d / "9.json").write_text(json.dumps({"pid": 9}), encoding="utf-8")
    assert session_state.read_all(tmp_path) == {}


def test_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert session_state.read_all(tmp_path) == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_session_state.py -q`

Expected: collection error, `ImportError: cannot import name 'session_state' from 'crr.adapters'` — same cause as Task 1, `crr.adapters` is an existing package.

- [ ] **Step 3: Implement**

Create `crr/adapters/session_state.py`:

```python
"""Claude Code's own per-process session state (spec 2026-08-09, Phase 1).

Claude Code writes `~/.claude/sessions/<pid>.json` for each running
process and updates it on every state change. The field that matters here
is ``bridgeSessionId``: non-null while the phone's Remote Control link is
up, null when it is down.

That it is authoritative was established by reading the shipped bundle,
not inferred. The bridge session lives in one module-level variable with
exactly one setter; that setter writes this field on every change, and
teardown calls it with null. The app's own user-facing copy defines
"connected" as the same variable.

Undocumented internal state, so every read degrades: a missing directory,
a corrupt file, or a missing field yields an absent entry or
``field_present=False`` — never a fabricated value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple


class SessionState(NamedTuple):
    pid: int | None
    bridge_session_id: str | None
    field_present: bool
    status: str | None
    waiting_for: str


def _bridge(data: dict) -> tuple[str | None, bool]:
    """Split ``bridgeSessionId`` into ``(value, readable)``.

    A null bridgeSessionId is an ANSWER ("the link is down"), so it must
    stay distinguishable from the field being absent — hence the flag
    rather than folding both into ``None``.

    Anything neither a string nor null is NOT an answer. Reporting it as a
    bare ``None`` with the flag set would have core classify the session
    ``unreachable``, and the watchdog would restart a live process on the
    strength of a value it could not parse.
    """
    if "bridgeSessionId" not in data:
        return None, False
    value = data["bridgeSessionId"]
    if value is None:
        return None, True
    if isinstance(value, str):
        return value, True
    return None, False


def read_all(home: Path | None = None) -> dict[str, SessionState]:
    """Newest state file per session id, as ``{session_id: SessionState}``.

    ONE directory scan, not one per card: the caller resolves this once per
    poll and injects the map. Newest-by-mtime wins because a session id can
    have several files from successive claude processes — observed live
    with three for one id — and only the newest describes the running one.
    """
    home = home or Path.home()
    sessions_dir = home / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return {}
    newest: dict[str, tuple[float, SessionState]] = {}
    for path in sessions_dir.glob("*.json"):
        try:
            mtime = path.stat().st_mtime
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue          # corrupt or unreadable: skip this file only
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        pid = data.get("pid")
        bridge, bridge_readable = _bridge(data)
        state = SessionState(
            pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            # A null bridgeSessionId is an ANSWER ("the link is down"), so it
            # must stay distinguishable from the field being absent entirely
            # (an older Claude Code, or a renamed field) — hence the separate
            # `field_present` rather than folding both into None.
            bridge_session_id=bridge,
            field_present=bridge_readable,
            status=data.get("status") if isinstance(data.get("status"), str) else None,
            waiting_for=data.get("waitingFor") if isinstance(data.get("waitingFor"), str) else "",
        )
        if sid not in newest or mtime > newest[sid][0]:
            newest[sid] = (mtime, state)
    return {sid: state for sid, (_m, state) in newest.items()}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_session_state.py -q && .venv/bin/lint-imports`

Expected: PASS and `Contracts: 1 kept, 0 broken`.

- [ ] **Step 5: Commit**

```bash
git add crr/adapters/session_state.py tests/test_session_state.py
git commit -m "feat(adapters): read Claude Code's per-process bridge state"
```

---

### Task 3: The card field

**Files:**
- Modify: `crr/core/contracts.py`, `crr/core/status.py`
- Test: `tests/test_contracts.py`, `tests/test_status.py`

**Interfaces:**
- Consumes: `reachability.reachability` (Task 1).
- Produces: `REMOTE_CONTROL_STATES == ("unknown", "reachable", "unreachable")`;
  card gains `waiting_for: str`; `SESSIONS_CONTRACT_VERSION` 11 → 12;
  `assemble_sessions(..., reachability_by_sid: Mapping[str, tuple[str, str]] | None = None)`
  mapping session id → `(remote_control_state, waiting_for)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contracts.py`:

```python
def test_remote_control_states_are_the_reachability_triple():
    assert contracts.REMOTE_CONTROL_STATES == ("unknown", "reachable", "unreachable")


def test_sessions_contract_version_is_12():
    # v12 replaces the record-counting remote_control enum and adds
    # waiting_for (spec 2026-08-09, Phases 1-3).
    assert contracts.SESSIONS_CONTRACT_VERSION == 12


def test_the_contract_enum_matches_the_core_one():
    # The enum now exists in two places — `reachability`'s constants and
    # this tuple — with nothing making them agree, so a rename in either
    # would diverge silently. Both are core, so this import is layering-legal.
    from crr.core import reachability as r
    assert set(contracts.REMOTE_CONTROL_STATES) == {r.REACHABLE, r.UNREACHABLE, r.UNKNOWN}


def test_waiting_for_is_a_contracted_card_field():
    assert "waiting_for" in contracts.SESSION_CARD_KEYS
    p = _sessions_payload()
    del p["sessions"][0]["waiting_for"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_sessions_payload(p)
```

Add to `tests/test_status.py`:

```python
def test_the_card_carries_the_injected_reachability():
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        reachability_by_sid={sid: ("unreachable", "permission prompt")})
    card = payload["sessions"][0]
    assert card["remote_control"] == "unreachable"
    assert card["waiting_for"] == "permission prompt"


def test_a_session_with_no_reachability_entry_is_unknown():
    # Nothing injected, or the adapter had no state file for it. Absence is
    # not evidence the bridge is down.
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions([_entry(42, sid)], FakeBoot(), FakeProbe())
    assert payload["sessions"][0]["remote_control"] == "unknown"
    assert payload["sessions"][0]["waiting_for"] == ""


def test_a_reachability_card_survives_its_own_validator():
    from crr.core import contracts
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    payload = assemble_sessions(
        [_entry(42, sid)], FakeBoot(), FakeProbe(),
        reachability_by_sid={sid: ("reachable", "")})
    contracts.validate_sessions_payload(payload)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_contracts.py tests/test_status.py -q -k "reachab or waiting_for or version_is_12"`

Expected: FAIL — `("unknown","off","ok","dropped") != ("unknown","reachable","unreachable")`, `11 != 12`, and `assemble_sessions() got an unexpected keyword argument 'reachability_by_sid'`.

- [ ] **Step 3: Implement**

In `crr/core/contracts.py`, replace the enum (keep the comment style):

```python
# Whether this session's phone link is up (spec 2026-08-09, Phases 1-3).
# Sourced from Claude Code's own `bridgeSessionId`, not inferred from
# transcript records. "unknown" is every failure route — a stale or
# recycled pid, a missing field, no state file at all — because an
# unreadable signal must not become a positive claim.
REMOTE_CONTROL_STATES = ("unknown", "reachable", "unreachable")
```

Add `"waiting_for"` to `SESSION_CARD_KEYS`, and in `validate_session_card`
add `_require_type(card["waiting_for"], str, "session 'waiting_for'")`.

Add the ledger entry and bump:

```python
# v12 replaces `remote_control`'s enum — the record-counting off/ok/dropped
# gives way to reachable/unreachable sourced from Claude Code's own
# bridgeSessionId — and adds `waiting_for` (spec 2026-08-09, Phases 1-3)
SESSIONS_CONTRACT_VERSION = 12
```

In `crr/core/status.py`: add the parameter
`reachability_by_sid: Mapping[str, tuple[str, str]] | None = None`, replace
the `remote_control` card value with a lookup defaulting to
`("unknown", "")`, add `"waiting_for"` to the card, and drop the
`_bridge_state` import and the `bridge_stale_records` parameter. Also
update the module docstring's `(contract v11)` → `(contract v12)` — the
ledger guard asserts it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q`

Expected (MEASURED after Task 3 landed — the first draft guessed this
wrong in both directions): **11 failures, one root cause** — every
`assemble_sessions(` call site still passes the removed
`bridge_stale_records`. Nine in `tests/test_cli.py`, one in
`tests/test_e2e_linux.py`, one in
`tests/test_priors.py::test_assemble_sessions_defaults_come_from_config`,
which indexes the removed parameter and raises `KeyError`.

`tests/test_web.py` and `tests/test_revive_bridge.py` **pass** — the first
asserts on `page.html` source strings (untouched until Task 6), the second
exercises `_kick_dropped_bridges`, which calls `bridge_state` independently
of `status.py` (untouched until Task 4). **Do not fix any of them here.**

Also update `_session_card()` (`tests/test_contracts.py:41`): adding
`waiting_for` to `SESSION_CARD_KEYS` goes through `_require_exact_keys`, so
every `_sessions_payload()` validation in that file breaks until the
fixture carries the new key — and the plan's own `del`-based test has
nothing to delete otherwise. Confirm `tests/test_contracts.py`, `tests/test_status.py` and
`tests/test_version_ledger.py` pass, and list the remaining failures in
your report; Tasks 4–6 own them.

- [ ] **Step 5: Commit**

Use `--no-verify` for this one commit only, because the tree is
deliberately red between Tasks 3 and 6:

```bash
git add crr/core/contracts.py crr/core/status.py tests/test_contracts.py tests/test_status.py
git commit --no-verify -m "feat(contracts): sessions v12 — reachability replaces the bridge enum"
```

State in your report that you used `--no-verify` and why.

---

### Task 4: The watchdog kick rule

**Files:**
- Modify: `crr/cli.py` (`_kick_dropped_bridges`)
- Test: `tests/test_revive_bridge.py`

**Interfaces:**
- Consumes: `reachability.may_kick` (Task 1),
  `session_state.read_all` (Task 2).
- Produces: `_kick_dropped_bridges(..., read_session_state=session_state.read_all)`
  — the injection seam its tests use.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_unreachable_idle_session_is_kicked(tmp_path):
    # The case the old detector could never reach: idle, so it wrote no
    # transcript records and the record counter never advanced.
    ...  # follow the existing tests' shape in this file
    assert recorder.calls != []


def test_an_unreachable_waiting_session_is_kicked(tmp_path):
    # THE deadlock-breaker: blocked on a permission prompt, so its tail is
    # mid-turn forever. The old boundary guard vetoed exactly this.
    ...
    assert recorder.calls != []


@pytest.mark.parametrize("status", ["busy", "shell"])
def test_an_unreachable_but_working_session_is_never_kicked(tmp_path, status):
    ...
    assert recorder.calls == []


def test_a_reachable_session_is_never_kicked(tmp_path):
    ...
    assert recorder.calls == []


def test_an_unknown_reachability_is_never_kicked(tmp_path):
    # No state file, a stale pid, or a missing field. Absence of evidence.
    ...
    assert recorder.calls == []
```

Write these out fully following the existing tests in
`tests/test_revive_bridge.py` — they already build a journal entry, a
`FakeBoot`/`FakeProbe`, and a `_Recorder` for `kick`. Replace the
`read_tail_facts` injection with a `read_session_state` one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_revive_bridge.py -q`

Expected: FAIL — the new injection parameter does not exist yet.

- [ ] **Step 3: Implement**

In `_kick_dropped_bridges`: replace the `read_tail_facts` +
`bridge.bridge_state` step with one `read_session_state()` call before the
loop and a `reachability.reachability(...)` per entry. Replace the
`takeover.ready_to_take_over` gate with `reachability.may_kick(status)`,
**except** keep `ready_to_take_over` as an additional requirement when
`status == "idle"` (two independent signals must agree before signalling a
live process); skip it entirely when `status == "waiting"`, which never
reaches a boundary.

`pid_matched` comes from `controller.claude_groups(entry["pid"])` — the
state file's pid must be one of this session's live claude processes.

**Known wart, decide deliberately:** `may_kick` returns `(True, "")` for
both `idle` and `waiting`, carrying no signal about which one needs the
`ready_to_take_over` corroboration. So `cli.py` must re-test `status ==
"idle"` itself, duplicating the vocabulary `reachability._KICKABLE` owns.
Either accept that knowingly and say so in a comment, or extend
`may_kick`'s return with a third element naming the reason — and if you do
the latter, update `tests/test_reachability.py` rather than working around
it. Do not leave the duplication silent.

Keep unchanged: the `remote_control_watch` gate, LIVE-only, the duplicate-sid
guard, `autokick_for`, the cooldown/attempt cap, fail-closed on a degraded
settings or kick-history store, and `mutation_lock` around the kick.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_revive_bridge.py -q`

Expected: PASS. The wider suite is still red (Tasks 5–6).

- [ ] **Step 5: Commit**

```bash
git add crr/cli.py tests/test_revive_bridge.py
git commit --no-verify -m "feat(revive): kick on bridgeSessionId, not record counts"
```

---

### Task 5: Wire the adapter in, delete the old detector

**Files:**
- Modify: `crr/cli.py`, `crr/core/config.py`, `crr/adapters/transcript_source.py`,
  **`crr/core/status.py`** — `_empty_facts` still returns `bridge_seen` /
  `bridge_since`, deliberately left in Task 3 so the default kept mirroring
  the real adapter's shape. Removing them is yours, and without this file
  Task 5 cannot finish its stated scope.
- Delete: `crr/core/bridge.py`, `tests/test_bridge.py`
- Test: `tests/test_cli.py`, `tests/test_transcript_source.py`,
  `tests/test_config.py`, `tests/test_priors.py`, **`tests/test_e2e_linux.py`**
  (it is in the red set and nothing else will turn it green)

- [ ] **Step 1: Write the failing test**

```python
def test_status_json_reports_reachability_from_the_state_file(tmp_path, monkeypatch, capsys):
    """End to end through the composition root."""
    from crr.adapters import session_state, state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    # journal one entry, then fake read_all to report it unreachable
    ...
    assert payload["sessions"][0]["remote_control"] == "unreachable"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -q -k reachability`

Expected: FAIL — the card still reports `unknown` because nothing injects.

- [ ] **Step 3: Implement**

- **`grep -n "assemble_sessions(" crr/cli.py` and change every hit.** Three
  today, but `cli.py:1900` (`_cmd_whoami`'s helper) is exercised by NO test
  — no failure will point at it. The grep is load-bearing, not belt-and-braces.
- Add `_reachability_by_sid(entries, controller)` to `cli.py`: one
  `session_state.read_all()`, then per entry resolve `pid_matched` via
  `controller.claude_groups(entry["pid"])` and classify. Inject at all
  **three** `assemble_sessions(` call sites (`grep -n "assemble_sessions("`).
- Delete `crr/core/bridge.py` and `tests/test_bridge.py`.
- Remove `bridge_stale_records` and `bridge_scan_lines` from
  `config.DEFAULTS`, add a `# v15` ledger entry, bump
  `CONFIG_DEFAULTS_VERSION` 14 → 15.
- Remove `bridge_seen`/`bridge_since` from `read_tail_facts` and the
  bridge-marker branch of its backward walk, plus `BRIDGE_SCAN_LINES` and
  `transcript.is_bridge_marker` if it has no other caller (check first).
- Update every test asserting the removed keys.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q && .venv/bin/lint-imports`

Expected: fully green except `tests/test_web.py` (Task 6).

- [ ] **Step 5: Commit**

```bash
git add -u && git add crr/adapters/session_state.py
git commit --no-verify -m "feat(cli): wire reachability; delete the record-counting detector"
```

(`git add -u` is permitted here ONLY because this task deletes files;
verify with `git status --short` that nothing unexpected is staged.)

---

### Task 6: The dashboard

**Files:**
- Modify: `crr/core/page.html`, `crr/core/web.py`
- Test: `tests/test_web.py`, `tests/test_page_version_guard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_page_renders_the_not_connected_badge():
    page = web.load_page()
    assert 'phone: not connected' in page
    assert 's.remote_control === "unreachable"' in page


def test_page_shows_what_a_waiting_session_is_blocked_on():
    assert "waiting on you" in web.load_page()


def test_page_has_no_dropped_or_off_remote_control_branches():
    page = web.load_page()
    assert 's.remote_control === "dropped"' not in page
    assert 's.remote_control === "off"' not in page


def test_reachable_renders_no_badge():
    # The common case must stay silent, or every card carries a chip.
    assert 's.remote_control === "reachable"' not in web.load_page()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -q -k "not_connected or waiting_on or dropped or reachable"`

- [ ] **Step 3: Implement**

Replace the `dropped`/`unknown` render branches with `unreachable`
(`phone: not connected`, red) and `unknown` (`phone: unknown`, muted). When
`s.waiting_for` is non-empty append `waiting on you` beside the badge.
Update the `remote control` key group's terms and help text. Bump
`PAGE_VERSION` **after re-checking the current value**, and update
`PAGE_PINS` in `tests/test_page_version_guard.py` — that guard now fails
otherwise, and its message gives you the exact line.

- [ ] **Step 4: Run everything**

```bash
.venv/bin/pytest -q && .venv/bin/lint-imports
```

Then the JS gate:

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

Expected: fully green, all blocks `OK`. **The tree must be green from here
on** — this is the last task that may leave it red.

- [ ] **Step 5: Commit** (no `--no-verify`; the gate must pass)

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py tests/test_page_version_guard.py
git commit -m "feat(web): the card says whether the phone can reach this session"
```

---

### Task 7: The transition counter

**Files:**
- Modify: `crr/core/bridge_kicks.py`, `crr/cli.py`
- Test: `tests/test_kick_lineage.py`

The spec's known gap — an established bridge that errors *without* teardown
leaves a stale session id, and the detector misses it — must be countable
rather than a story.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_observed_reachable_to_unreachable_transition_is_counted(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_transition(SID, now=1000.0)
    assert store.observed_transitions() == 1


def test_transitions_accumulate_across_sweeps(tmp_path):
    store = bridge_kicks.KickHistoryStore(tmp_path)
    for t in (1000.0, 2000.0):
        store.record_transition(SID, now=t)
    assert store.observed_transitions() == 2
    assert store.last_transition_at() == 2000.0


def test_a_legacy_file_reports_zero_transitions(tmp_path):
    (tmp_path / bridge_kicks.FILENAME).write_text('{"sessions": {}}', encoding="utf-8")
    assert bridge_kicks.KickHistoryStore(tmp_path).observed_transitions() == 0
```

- [ ] **Step 2–4:** implement `observed_transitions()` / `last_transition_at()` /
  `record_transition()` as top-level keys in the existing store (it is
  already versioned and degrades honestly); call `record_transition` from
  `_kick_dropped_bridges` when a sid's reachability moves reachable →
  unreachable; surface both in `crr doctor`. Run `.venv/bin/pytest -q` and
  `.venv/bin/lint-imports`.

- [ ] **Step 5: Commit**

```bash
git add crr/core/bridge_kicks.py crr/cli.py tests/test_kick_lineage.py
git commit -m "feat(doctor): count observed reachable->unreachable transitions"
```

---

### Task 8: Merge, deploy, verify on the real machine

- [ ] **Step 1: Confirm the old detector is gone**

```bash
git diff main...HEAD --stat
test ! -f crr/core/bridge.py && echo "bridge.py deleted"
grep -rn "bridge_stale_records\|bridge_scan_lines\|bridge_since" crr/ || echo "no references remain"
```

- [ ] **Step 2: Confirm nothing else regressed**

```bash
.venv/bin/pytest -q && .venv/bin/lint-imports
```

- [ ] **Step 3: Merge on local green**

```bash
git fetch -q origin && git rev-parse --short origin/main   # re-check it has not moved
git checkout main && git merge --no-ff worktree-<branch>
.venv/bin/python -m pytest -q && .venv/bin/lint-imports
```

- [ ] **Step 4: Deploy and verify against the live machine**

```bash
systemctl --user restart crr-web.service && sleep 2
curl -s http://127.0.0.1:8377/api/version
curl -s http://127.0.0.1:8377/api/sessions | python3 -c "
import json,sys,collections
d=json.load(sys.stdin)
print('contract', d['contract'])
print(dict(collections.Counter(s['remote_control'] for s in d['sessions'])))"
```

Cross-check against Claude Code's own view — they must agree:

```bash
python3 - <<'EOF'
import json, glob, os, collections
best = {}
for f in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
    try: d = json.load(open(f))
    except Exception: continue
    sid = d.get("sessionId")
    if not sid: continue
    m = os.path.getmtime(f)
    if sid not in best or m > best[sid][0]: best[sid] = (m, d)
c = collections.Counter("connected" if d.get("bridgeSessionId") else "not" for _m, d in best.values())
print(dict(c))
EOF
```

- [ ] **Step 5: Verify the watchdog kicks nothing it should not**

```bash
.venv/bin/crr revive 2>&1 | tail -3
ps -eo args | grep -c "^claude "
```

Expected: the claude process count is unchanged. Report plainly whether any
kick fired, and if one did, whether it was correct.

- [ ] **Step 6: Push**

```bash
git push origin main
```

---

## Verified while writing this plan

- `transcript.is_bridge_marker` has exactly **one** caller,
  `crr/adapters/transcript_source.py:562`, inside the backward walk Task 5
  removes. So it goes too, along with its tests.
- `tests/test_revive_bridge.py` injects via a `_facts(bridge_seen, bridge_since)`
  helper at line 64 and a `_signal(tail_kind, mtime)` at line 72. Task 4
  replaces `_facts` with a session-state equivalent; `_signal` survives,
  because `ready_to_take_over` is still required for `status == "idle"`.
- **5** assertions in `tests/test_web.py` mention `dropped`/`off`. Task 6
  owns all five.

## Report honestly

The three Phase 0 implementers each found a real defect in their plan —
including two the plan asserted confidently and got wrong. Expect the same
here. Say what you found, and be specific about where this plan was wrong;
that is worth more than a clean report.

Known-shaky areas, flagged so you check rather than trust:

- Task 4's guard chain is described in prose, not code. The existing
  `_kick_dropped_bridges` is ~120 lines with eight ordered guards; read it
  before editing and report if the described replacement does not fit.
- Task 5 claims exactly three `assemble_sessions(` call sites. That was
  true for Phase 0; re-run the grep.
- The `--no-verify` commits in Tasks 3–5 leave the tree red on purpose.
  If you find a way to keep it green without contorting the work, do that
  instead and say so.
