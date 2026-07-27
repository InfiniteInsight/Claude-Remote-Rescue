# Claude-Remote-Rescue — Autonomous Completion Handoff

**Audience:** an autonomous agent (Fable) tasked with finishing crr in one pass.
**Author of this handoff:** the Opus session that built Slices 1 + 2a of task #4.
**Prime directive:** complete all remaining **code**, merging each unit on
local-CI-green, without babysitting the human. STOP only for the two
genuinely human-gated operational steps (the production **cutover** and public
**release/announcement**) — for those, write runbooks; do **not** execute.

---

## 0. ABSOLUTE SAFETY RULES (non-negotiable; a violation is a critical failure)

This machine (HedyLamarr, WSL2/Ubuntu) runs the user's **production** tool
**ccresume** right now:
- **6 live `cc-*` tmux sessions** — the user's real work.
- **`ccresume-web.service` on port 8377** — the live tailnet dashboard.
- **`ccresume-watchdog.timer`** — its revival timer.

Rules:
1. **NEVER disrupt ccresume or the 6 `cc-*` sessions.** Never touch them.
2. **NEVER bind crr to port 8377.** Never `git clean -fdx` the repo.
3. **NEVER live-run `kick`/`close`/`reopen`/`revive` against a production
   session. NEVER perform the cutover. NEVER reboot the machine.**
4. **All on-hardware testing is ISOLATED:** scratch `XDG_STATE_HOME`, scratch
   `TMUX_TMPDIR` (its own tmux socket), a non-8377 port, a **fake `claude`**
   (a script that `exec sleep 1000000`), and `fish --no-config`. Confirm
   production is intact **before and after** every test, and **tear down every
   scratch artifact** ("no dangling"). Detect stray processes with
   `ps -C sleep` (matches by name); **never** `pkill -f "..."` — it
   self-matches its own shell and produces false positives / kills the wrong
   thing (a real bug we hit).
5. If a step would touch production or is irreversible and you are unsure,
   **STOP and write it into the runbook** instead of doing it.

There is a proven isolation recipe in the git history (the #8 reboot test):
scratch dirs under `~/.local/state/`, a hand-written `crr-revive` unit with
baked `XDG_STATE_HOME` + `TMUX_TMPDIR` + a fake-`claude` PATH. Reuse that
pattern for any live check.

---

## 1. Codebase rules (read `AGENTS.md` and `CLAUDE.md` first — they govern)

- **Python 3.12, stdlib only** (zero runtime dependencies). Dev CLI:
  `.venv/bin/crr`. Tests: `.venv/bin/pytest`.
- **One-way layering, CI-enforced:** `crr.cli → crr.adapters → crr.core`; core
  imports neither adapters nor cli. Interfaces are Protocols in
  `crr/core/ports.py`; adapters are selected only in `crr.cli`.
  `.venv/bin/lint-imports` MUST print `KEPT`.
- **TDD, no exceptions:** write the failing test, run it, watch it fail for the
  *right* reason, then write minimal code to pass. Never write production code
  first.
- **Versioned contracts:** bump the relevant constant in `crr/core/contracts.py`
  (+ its validator + a test) on any stored/served payload change. Bump
  `PAGE_VERSION` in `crr/core/web.py` on ANY `crr/core/page.html` change, and
  keep the `node --check` page-JS gate green (`.venv/bin/pytest -k node`).
- **Untrusted fields reach the DOM via `textContent` only** (cwd, last_prompt,
  model, ids) — never innerHTML.
- **Honest calibration:** never overstate verification. Say "unit-tested
  cross-OS; live-verified on Linux/WSL" — never claim macOS works without a Mac.
- **Cross-OS parity is a hard user requirement:** every feature ships for
  Linux/WSL/macOS and all three shims (fish/bash/zsh) *in the same change*.
  Only *live* macOS/Windows runs may await hardware; the code and unit tests
  are complete and portable regardless.

## 2. Merge process (GitHub Actions is BILLING-BLOCKED — jobs cannot run)

Merge each unit on **local-CI-green** = `.venv/bin/pytest -q` +
`.venv/bin/lint-imports` (`KEPT`) + the `node --check` gate (when page.html
changed). Flow (matches PRs #21–#25):

1. Work on a feature branch (never commit straight to `main`).
2. `git checkout main && git merge --no-ff <branch>` → **re-run the full gates
   on the merged result** → `git push origin main`.
3. `gh pr create --base main --head <branch> ...` — GitHub auto-marks it merged
   once the commits are on `main`.
4. Delete the branch (local + remote).

Commit trailer (use YOUR identity, not the Opus session):
```
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
```

## 3. The method (use it — it produced Slices 1 + 2a cleanly)

For each remaining unit, run the Superpowers skills in sequence:
`brainstorming` → `writing-plans` → `subagent-driven-development` →
`finishing-a-development-branch`. Notes:
- **Brainstorming:** resolve design yourself. Since the user wants NO
  babysitting, DO NOT block them with questions — pick the sensible default
  aligned with `DESIGN.md` and the existing code, **record it in the spec**,
  and proceed. Only the human-gated items in §5 are exceptions.
- **Subagent-driven:** fresh subagent per task (sonnet for logic, haiku for
  pure transcription), per-task spec+quality review, an **opus** final
  whole-branch review, and the fix loop. Use the SDD scripts under
  `~/.claude/plugins/cache/superpowers-marketplace/superpowers/*/skills/subagent-driven-development/scripts/`
  (`sdd-workspace`, `task-brief`, `review-package`) and keep a ledger at the
  printed workspace path. The workspace lives under `.superpowers/` (already
  git-ignored).
- Call **`advisor()`** before committing to an approach and before declaring
  each unit done.
- Do not pause between units to check in. Execute continuously.

---

## 4. THE WORK — build all of this autonomously, in order

### 4.1 — #4 Slice 2b: the shim repair loop  ← DO THIS FIRST; it closes #4

The server side (Slices 1 + 2a) is merged: `ops.kick`/`ops.close` kill
claude's process group and arm a 3-state flag; `crr repair-check --pid
[--clear]` reads/clears it. **The authoritative spec is**
`docs/superpowers/specs/2026-07-27-kick-close-repair-loop-design.md` — read
"The flag protocol (3-state)", "Shim repair loop", and especially the four
numbered **"Slice-2b hard requirements"**.

Build the repair loop into `crr/shims/crr.fish`, `crr/shims/crr.bash`,
`crr/shims/crr.zsh`. The `claude()` wrapper wraps `command claude` in a loop:
1. At wrapper start, clear any stale flag (`crr repair-check --pid $pid --clear`).
2. Run `command claude …`; capture the exit code.
3. `crr repair-check --pid $pid`, then branch on the output:
   - **`relaunch <sid>`** → silently `command claude --resume <sid>`; loop.
   - **`close`** → `crr claude-exit --pid $pid` (deregister), then `exit` the
     shell (closes the tab/pane/ssh session). Terminal; no loop.
   - **empty (absent) + nonzero exit** → print
     `crr: claude exited unexpectedly (<code>). Resume this conversation?
     [Y/n]` and read; **yes / timeout / no-tty → resume**, explicit **no →
     stop**. Loop on resume.
   - **empty + exit 0** → return to the prompt (current clean-exit behavior).
4. **≤ 2 resume attempts** per invocation (give-up guard); the `close` branch
   is not subject to the cap.

Honor all four hard requirements: unknown kind → treat as absent; `relaunch`
with no sid → treat as absent (never `--resume ` with an empty arg); parse the
line yourself (fish `string split ' '`; bash/zsh `read kind sid`); read and
clear are two calls (design read-then-clear, accept the small window). fish has
no native timed `read` — fall back to a blocking read and rely on the
no-tty→resume rule.

Test in `tests/test_shims.py` (gated per installed shell — mirror the existing
gating). Then a **live smoke test IN ISOLATION** (scratch state, fake claude,
`fish --no-config`, scratch tmux socket): prove kick→silent-resume,
close→shell-exits, crash→offer, clean→return — with the 6 `cc-*` sessions
provably untouched throughout. Merge. **Mark task #4 complete.**

### 4.2 — #10: dashboard "de-tmux" button

Re-home a revived (detached-`tmux`) session into a plain **visible tab**, then
drop the tmux wrapper. Reuse the tab-spawn adapters and
`crr.core.reviver.attach_argv`. Brainstorm the exact shape (most likely a new
`/api/action` op `detmux` that opens a tab attached to `crr-<sid8>` and, on
success, clears the session's tmux bookkeeping), spec it, plan it, build it
(core op in `ops.py` + CLI + `/api/action` entry + a dashboard button, gated to
the states where a tmux session exists + `PAGE_VERSION` bump), test, merge. It
was gated behind #8, which is **done**. Make reasonable design choices and
document them in the spec.

### 4.3 — #9: packaging artifacts (the part you CAN build)

Author, as files in the repo (e.g. under `packaging/`): a **Homebrew formula**
and an **AUR `PKGBUILD`**, plus release polish (version bump if warranted, a
`CHANGELOG.md`). Lint/validate them as far as possible offline. Merge them.
**Do NOT** submit to Homebrew/AUR, cut a GitHub release/tag, or announce —
those need a published release and the user's accounts (see §5).

---

## 5. STOP — human-gated. Do NOT execute. Write `docs/RUNBOOK-cutover-and-release.md`

These touch the user's live production or personal accounts. Produce an exact,
**reversible**, step-by-step runbook (with rollback at each step), but run
none of it:

1. **The production cutover (Path A):** cycle the 6 `cc-*` sessions under
   crr's shim (they are ccresume-journaled with `cc-*` names and the ccresume
   `claude()` shim, so crr cannot hot-adopt them — they must be restarted
   under crr's shim); move the dashboard from ccresume:8377 to crr; retire
   ccresume, keeping it as the rollback net until the user is satisfied. The
   `#8` reboot test already proved crr's watchdog survives a real reboot, so
   the mechanism is trusted — but the migration itself disrupts live sessions
   and must be done with the user present.
2. **Publishing:** the GitHub release/tag, Homebrew/AUR submission, and public
   announcement.

## 6. Definition of done

- Slice 2b built, tested, live-verified in isolation, merged; **#4 complete**.
- #10 built, tested, merged; **#10 complete**.
- Packaging files written and merged.
- `docs/RUNBOOK-cutover-and-release.md` written.
- Production (ccresume, port 8377, the 6 `cc-*` sessions) left EXACTLY as
  found — verify and state so.
- Final report to the user: PRs merged, what is unit-tested vs live-verified
  (and on which OS/shell), and the two human-gated items awaiting them.
