# Plan — `crr adopt --takeover` (Implementation)

> Execute subagent-driven, TDD (watch red first). Read CURRENT code before
> editing (this repo evolves). A pre-commit hook runs the full suite + lint on
> every commit — the tree must be green to commit. Layering `cli→adapters→core`;
> `.venv/bin/lint-imports` = `KEPT`. stdlib only.

**Spec:** `docs/superpowers/specs/2026-08-03-adopt-takeover.md` (read it first).

**Base:** branch `feat/adopt-takeover` off `main` (HEAD `1752b86` family).

**No page change** — CLI-only v1; do NOT touch `page.html` or `PAGE_VERSION`.
**No contract change** — no session-card field added, so `SESSIONS_CONTRACT_VERSION`
stays. (Config DEFAULTS version DOES bump — Task 1.)

---

### Task 1: Core — `turn_boundary`, `ready_to_take_over`, config thresholds

**Files:** `crr/core/transcript.py`, create `crr/core/takeover.py`,
`crr/core/config.py`, tests `tests/test_transcript.py`,
`tests/test_takeover.py`, `tests/test_config.py`.

**Deliverable:**
- `crr/core/transcript.py::turn_boundary(record) -> str`:
  - `"assistant-end"` — `type=="assistant"`, `message.role=="assistant"`,
    NOT `<synthetic>`, `message.stop_reason == "end_turn"`.
  - `"mid-turn"` — an assistant record with any other `stop_reason` (empirically
    `"tool_use"`, incl. records whose only content is text/thinking), OR a
    `type=="user"` record carrying `toolUseResult` (a tool-result turn).
  - `"user-prompt"` — a real user prompt (reuse the existing `extract_prompt`
    honesty: a user turn with real text, no `toolUseResult`).
  - `"other"` — everything else (permission-mode, pr-link, bridge-session,
    isMeta, malformed).
- `crr/core/takeover.py::ready_to_take_over(seconds_idle: float, tail_kind: str,
  *, idle_window: float) -> bool` = `seconds_idle >= idle_window and tail_kind
  == "assistant-end"`. Pure; no I/O/clock/sleep. Docstring explains WHY only
  `assistant-end` is safe (spec: response-always-follows-a-prompt).
- `crr/core/config.py` DEFAULTS: add `"takeover_idle_seconds": 20.0`,
  `"takeover_max_wait_seconds": 180.0`, `"takeover_poll_seconds": 2.0`. Bump
  `CONFIG_DEFAULTS_VERSION`.

> **NOTE (already implemented + refined in commits `8fd1bd4` + follow-up):**
> Task 1 is done. `turn_boundary` classifies `<synthetic>` assistant records
> as `"other"` (transparent, matching `extract_model`); `takeover_idle_seconds`
> is `20.0`. Task 2/3 build on that.

**Steps:** read `extract_prompt`/`extract_model`/`_assistant_text` (reuse their
role/synthetic/`toolUseResult` conventions — do NOT reinvent the user-turn
noise checks) and the config version constant. Failing tests first
(turn_boundary on each real-shaped record kind incl. an assistant `tool_use`
with text content → "mid-turn"; predicate boundaries; config keys present).
Watch fail. Implement. Green + KEPT. Commit.

---

### Task 2: Adapters — resolve the live process + sample the takeover signal

**Files:** `crr/core/ports.py` (add port method + `ResumeProcess` type),
`crr/adapters/process_probe.py`, `crr/adapters/transcript_source.py`, tests
`tests/test_process_probe.py`, `tests/test_transcript_source.py`.

**Deliverable:**
- `crr/core/ports.py`: `class ResumeProcess(NamedTuple): pid: int; ppid: int;
  pgid: int`. Extend the `ProcessController` Protocol with
  `find_resume_process(session_id: str) -> ResumeProcess | None`.
- `crr/adapters/process_probe.py::PsProcessController.find_resume_process`:
  one `ps` snapshot with FULL args (the existing `_parse_ps_rows` truncates to
  argv0 for ancestry — this match needs the whole cmdline). Match a row whose
  args contain `claude --resume <session_id>` as an argv boundary (the sid is a
  full argv element — match it as such, not a loose substring, to avoid a
  prefix false-hit). Return the first match's `ResumeProcess(pid, ppid, pgid)`,
  else None. Ignore rows for a different sid or a non-`--resume` claude.
  Docstring: this sid-scoped `--resume <UUID>` match is a DIFFERENT specificity
  class from the broad `_is_claude_argv0` selector the kill-by-ancestry lesson
  warns against — one UUID, one conversation; the caller additionally guards
  by re-checking untracked and kills by the returned pgid.
- `crr/adapters/transcript_source.py::read_takeover_signal(session_id, home=None)
  -> dict`: `{"mtime": float, "tail_kind": str}`. **Read the TAIL first, THEN
  stat the mtime** (concurrent-append safety — see the spec: tail-first pairs
  a just-changed tail with a fresh mtime → `seconds_idle` small → keep waiting,
  the safe direction). A bounded backward read (`_reversed_lines`, early-exit)
  finds the newest record whose `turn_boundary` is not `"other"` → its
  `tail_kind` (synthetic/permission-mode/etc. are `"other"`, so the scan skips
  past them to the prior real turn). `""` if none found; `mtime=0.0` and
  `tail_kind=""` if the transcript is absent. Reuse the backward-read +
  per-line `json.loads` guard pattern from `read_tail_facts`, and call the
  core `transcript.turn_boundary` (adapters may import core).

**Steps:** read `_ps_snapshot_argv`/`_parse_ps_rows`/`_child_groups` and
`read_tail_facts`. Failing tests first: `find_resume_process` via a
monkeypatched `subprocess.run` returning canned `ps` stdout (match / wrong-sid
/ non-resume / none); `read_takeover_signal` against a written fake transcript
under a fake HOME (assistant-end tail, mid-turn tail, empty/absent). Watch
fail. Implement. Green + KEPT. Commit.

---

### Task 3: CLI — `crr adopt SID [--takeover]` orchestration

**Files:** `crr/cli.py`, `README.md`, tests `tests/test_cli.py`.

**Deliverable:**
- New subparser `crr adopt SID [--takeover] [--wait SECONDS]`:
  - Validate SID via `contracts.valid_session_id` (mirror `_cmd_discover`).
  - Plain (no `--takeover`): call the existing `_adopt(store, sd, sid)` — same
    output as `crr discover --adopt SID`. (`discover --adopt` stays untouched.)
  - `--takeover`: run `_takeover(...)` under `mutation_lock`. `--wait` overrides
    `takeover_max_wait_seconds` (default from config).
- `_takeover(store, sd, config, controller, flags, sid, *, max_wait) ->
  tuple[bool, str]` — the cli-owned orchestration (spec ordering):
  1. `proc = controller.find_resume_process(sid)`. None → refuse:
     `"no live 'claude --resume <sid>' found; adopt without --takeover, or exit
     it in its own terminal first"`. (Fresh-session home.)
  2. **Wait loop, refuse-fast** (real `time.sleep`, `time.time()` deadline).
     Each poll: `sig = transcript_source.read_takeover_signal(sid)`;
     `seconds_idle = time.time() - sig["mtime"]`.
     - `seconds_idle < idle_window` (`takeover_idle_seconds`) → still writing →
       keep polling; if past the `max_wait` deadline → refuse, **no kill, no
       flag**: `"still actively writing after Ns; not taking over"`.
     - `seconds_idle >= idle_window` → decide NOW:
       - `sig["tail_kind"] == "assistant-end"` (use
         `takeover.ready_to_take_over`) → break, proceed to kill.
       - else → refuse **immediately** (do NOT wait out the timeout), **no
         kill, no flag**: `"idle but parked at <tail_kind> — not a safe
         boundary to take over; finish or exit it manually"`.
     Every non-`assistant-end` branch is a refusal — refuse-fast never kills.
  3. **Re-check untracked** immediately before the kill (exclusion guard vs the
     resolve→kill race): the sid must NOT be in the journaled set now (reuse the
     `_discoverable_rows`/journal-scan path or a targeted `store` check). Now
     tracked → refuse.
  4. `flags.arm_close(proc.ppid)`, then `controller.terminate_group(proc.pgid,
     grace)`. Wrap the kill like `ops._signal_groups`: if it raises / no kill
     lands → `flags.clear(proc.ppid)` and refuse untouched. (Mirror
     `_reopen_ghost`'s rollback rule.)
  5. On a landed kill → `_adopt(store, sd, sid)` and return its result, prefixed
     to make the takeover explicit (e.g. `"took over 1a2b3c4d (stopped live pid
     N); "` + adopt message).
  - Inject `controller`, `flags`, `config`, and the clock/sleep so the happy
    path and every refusal are testable without real processes or wall-clock
    waits (a `sleep`/`clock` seam like the rest of cli uses, or a small
    injected pair). Keep the poll loop OUT of core.
- `README.md`: document `crr adopt` + `--takeover` (what it does, the
  live-process requirement, that it waits for a turn boundary, and that it is
  destructive/default-off). Note the fresh-session limitation.

**Steps:** read `_cmd_discover`/`_adopt`/`_cmd_web` provider wiring +
`mutation_lock` + how cli builds the `ProcessController`/`FlagStore` for
kick/close, and the existing sleep/clock seam. Failing tests first (happy-path
ordering via fakes; each refusal branch; flag rollback on failed kill;
re-check-untracked refusal). Watch fail. Implement. Full suite + KEPT. Commit.

---

## Done
- [ ] Full suite + `lint-imports` KEPT green.
- [ ] Opus whole-branch review; fix wave if needed.
- [ ] Re-verify `origin/main` unmoved; merge `feat/adopt-takeover` → main
      local-CI-green + push. **No service restart needed** (no page change).
- [ ] Summary notes CLI-only v1 + the fresh-session refusal limitation.
