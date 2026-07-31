# Restore-Prompt UX + Docs Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the last two buildable roadmap features: the Phase-3 restore-prompt UX (a new interactive shell offers to re-home conversations the watchdog rescued after a crash/reboot) and the Phase-5 docs site (static, dependency-free, GitHub-Pages-servable).

**Architecture:** A pure-core `rescue` module (candidate selection + per-boot prompt marker), two CLI commands (`crr rescued` to list, `crr rescue-check` for the one-shot interactive offer), a one-line interactive-gated hook in each shell shim, and a self-contained static HTML site under `docs/site/`.

**Tech Stack:** Python 3.12 stdlib only; fish/bash/zsh shims; hand-written HTML/CSS (no generators, no external assets).

## Global Constraints

- **Layering (CI-enforced):** `crr.cli → crr.adapters → crr.core`; core never imports adapters/cli. `.venv/bin/lint-imports` must print `KEPT`.
- **Zero runtime dependencies.** The docs site is plain HTML/CSS with no external requests (no CDN fonts/scripts) and no build step.
- **TDD:** failing test first, watch it fail for the right reason.
- **P5 (injectable priors):** every new timing/threshold constant joins `config.DEFAULTS` at introduction time. This plan introduces exactly one: `rescue_prompt_timeout_seconds` (default 15).
- **Shim lessons:** absolute-path invocation only; a missing/broken `crr` is a silent no-op (never error text into the prompt); the rescue-check call must NOT silence stdout (the prompt must reach the user) but MUST silence stderr; gate on interactive shells only.
- **Contracts:** the prompt marker is an opaque per-boot file like the relaunch flags — no versioned contract (same rationale as `flags.py`). No stored/served shape changes in this plan.
- **Local CI green before every commit:** `.venv/bin/pytest -q` AND `.venv/bin/lint-imports`.
- **Commit trailer:** `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Work on branch `feat/restore-prompt-docs`. Test runner `.venv/bin/pytest`; dev CLI `.venv/bin/crr`.
- **Safety:** this machine runs production crr (live sessions, port 8377). Tests never touch the real state dir, never spawn real tabs, never signal real processes — tmpdir state + fakes only, matching every existing test's convention.

---

### Task 1: Core rescue module + `crr rescued`

**Files:**
- Create: `crr/core/rescue.py`
- Modify: `crr/cli.py` (new `rescued` subcommand), `crr/core/config.py` (add `rescue_prompt_timeout_seconds`)
- Test: `tests/test_rescue.py` (new), `tests/test_config.py` (key present), `tests/test_cli.py` (command output)

**Interfaces:**
- Produces (Task 2 and 3 rely on these exact names):
  - `rescue.rescued_sessions(entries, current_boot, live_tmux) -> list[dict]` — pure: journal entries with `claude` non-None, `boot_id != current_boot`, `tmux_session` truthy AND in `live_tmux`. Sorted by pid.
  - `rescue.marker_path(state_dir, boot_id) -> Path` — `<state_dir>/rescue-prompted-<boot_id>` (boot_id is a kernel-supplied UUID, filename-safe).
  - `rescue.already_prompted(state_dir, boot_id) -> bool`
  - `rescue.mark_prompted(state_dir, boot_id) -> None` — creates the marker (parents ok, content empty); also opportunistically unlinks `rescue-prompted-*` files for OTHER boot ids (stale markers from previous boots).
  - CLI `crr rescued`: prints one line per rescued session `#<pid> · <sid8> <cwd> → <tmux_session>` and a trailer `attach: tmux attach -t <name> · dashboard: Reopen/De-tmux`; prints `no rescued sessions` and exits 0 when none.

- [ ] **Step 1: Write the failing tests**

`tests/test_rescue.py` (build entries with the same `new_entry`/dict shapes existing tests use; sids are full UUIDs):

```python
def test_rescued_sessions_selects_prior_boot_tmux_parked_only():
    """Phase-3 restore prompt: a candidate is a prior-boot entry whose
    conversation the reviver parked in a LIVE tmux session."""
    e_ok      = _entry(pid=2, boot="old", claude=True,  tmux="crr-aaaaaaaa")
    e_sameboot= _entry(pid=3, boot="cur", claude=True,  tmux="crr-bbbbbbbb")
    e_noclaude= _entry(pid=4, boot="old", claude=False, tmux="crr-cccccccc")
    e_notmux  = _entry(pid=5, boot="old", claude=True,  tmux=None)
    e_deadtmux= _entry(pid=6, boot="old", claude=True,  tmux="crr-dddddddd")
    out = rescue.rescued_sessions(
        [e_deadtmux, e_ok, e_sameboot, e_noclaude, e_notmux],
        current_boot="cur", live_tmux={"crr-aaaaaaaa", "crr-bbbbbbbb"})
    assert [e["pid"] for e in out] == [2]

def test_marker_roundtrip_and_stale_cleanup(tmp_path):
    assert not rescue.already_prompted(tmp_path, "boot-1")
    rescue.mark_prompted(tmp_path, "boot-1")
    assert rescue.already_prompted(tmp_path, "boot-1")
    rescue.mark_prompted(tmp_path, "boot-2")   # new boot
    assert rescue.already_prompted(tmp_path, "boot-2")
    assert not rescue.already_prompted(tmp_path, "boot-1")  # stale marker removed
```

`tests/test_config.py`: add `rescue_prompt_timeout_seconds` to the expected-keys/floor test and assert its default is 15.

`tests/test_cli.py`:

```python
def test_rescued_lists_prior_boot_parked_sessions(crr_state, monkeypatch, capsys):
    # journal one prior-boot entry with tmux_session crr-<sid8>; fake boot adapter
    # returns a different current boot; fake tmux list_sessions returns {name}.
    rc = cli.main(["rescued"])
    out = capsys.readouterr().out
    assert rc == 0 and "crr-" in out and "tmux attach" in out

def test_rescued_reports_none(crr_state, capsys):
    assert cli.main(["rescued"]) == 0
    assert "no rescued sessions" in capsys.readouterr().out
```

(Mirror the file's existing fixture technique for state-dir + boot/tmux monkeypatching.)

- [ ] **Step 2: Run, watch each fail for the right reason** (ImportError / KeyError / argparse error).

- [ ] **Step 3: Implement**

`crr/core/rescue.py`:

```python
"""Rescued-session selection + the per-boot restore-prompt marker.

A "rescued" session is a journal entry from a PREVIOUS boot whose
conversation the reviver parked in a currently-live tmux session: crashed
shell, revived claude, awaiting re-homing into something visible. The
restore prompt (Phase 3 UX) offers exactly that set once per boot.

The marker is an opaque per-boot file (like the relaunch flags — no
versioned contract): its existence means "this boot's prompt was already
shown/answered"; markers from other boots are stale and swept on write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

_MARKER_PREFIX = "rescue-prompted-"


def rescued_sessions(
    entries: Iterable[Mapping[str, Any]],
    current_boot: str,
    live_tmux: set[str],
) -> list[dict]:
    out = [
        dict(e) for e in entries
        if e.get("claude") is not None
        and e["boot_id"] != current_boot
        and e.get("tmux_session")
        and e["tmux_session"] in live_tmux
    ]
    return sorted(out, key=lambda e: e["pid"])


def marker_path(state_dir: Path | str, boot_id: str) -> Path:
    return Path(state_dir) / f"{_MARKER_PREFIX}{boot_id}"


def already_prompted(state_dir: Path | str, boot_id: str) -> bool:
    return marker_path(state_dir, boot_id).exists()


def mark_prompted(state_dir: Path | str, boot_id: str) -> None:
    target = marker_path(state_dir, boot_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    for stale in target.parent.glob(f"{_MARKER_PREFIX}*"):
        if stale != target:
            stale.unlink(missing_ok=True)
    target.touch()
```

`crr/core/config.py`: add to DEFAULTS under a `# restore prompt` comment: `"rescue_prompt_timeout_seconds": 15,  # [Y/n] wait before defaulting to "not now"`.

`crr/cli.py` `_cmd_rescued` (composition root wires adapters, mirrors `_cmd_status`'s error handling):

```python
def _cmd_rescued(_args: argparse.Namespace) -> int:
    config = _load_config()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr rescued: {exc}", file=sys.stderr)
        return 2
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    live = tmux_spawner.list_sessions() if tmux_spawner.available() else set()
    store = JournalStore(state_dir.state_dir())
    found = rescue.rescued_sessions(store.scan().entries, boot.current(), live)
    if not found:
        print("no rescued sessions")
        return 0
    for e in found:
        sid8 = e["claude"]["session_id"][:8]
        print(f"#{e['pid']} · {sid8} {e['cwd']} → {e['tmux_session']}")
    print("attach: tmux attach -t <name> · dashboard: Reopen/De-tmux")
    return 0
```

Register the subparser (`help="list conversations rescued from a previous boot (awaiting re-home)"`). Import `rescue` in the existing `from crr.core import ...` line.

- [ ] **Step 4: Full gates** — `.venv/bin/pytest -q && .venv/bin/lint-imports`.
- [ ] **Step 5: Commit** — `feat(rescue): rescued-session core + crr rescued listing`.

---

### Task 2: `crr rescue-check` — the one-shot interactive offer

**Files:**
- Modify: `crr/cli.py` (new `rescue-check` subcommand + `_cmd_rescue_check`), `DESIGN.md` (Claude wrapper / session-ops area: one paragraph documenting the Phase-3 prompt), `ROADMAP.md` (mark the Phase-3 bullet built), `CHANGELOG.md`
- Test: `tests/test_cli.py`

**Behavior (decided; implement exactly):** `crr rescue-check` is called by the shims on interactive shell start. Outcomes:
1. Silent no-op (exit 0, no output) when: marker for the current boot exists, OR no rescued sessions, OR stdin/stdout is not a tty (`sys.stdin.isatty() and sys.stdout.isatty()` both required for prompting).
2. Headless (no tab spawner available via `_tab_spawner(config)`): print the one-line notice `crr: N conversation(s) rescued from the last reboot — 'crr rescued' lists them; attach with: tmux attach -t <name>` , write the marker, exit 0. No prompt (there are no tabs to offer).
3. Interactive with a tab spawner: print `crr: N conversation(s) rescued from the last reboot. Open them in terminal tabs? [Y/n] ` (no newline), wait up to `rescue_prompt_timeout_seconds` for a line on stdin via `select.select([sys.stdin], [], [], timeout)`:
   - Answer `y`/`Y`/empty line → for each rescued pid, run the detmux flow (`ops.detmux(...)` under `mutation_lock`, same wiring as `_cmd_detmux`) and print each result message; then write the marker.
   - Answer `n`/`N`, any other input, timeout, or EOF → print `not now — 'crr rescued' lists them`, write the marker.
   - Either way exit 0. The marker means at most ONE shell ever prompts per boot.
4. Any unexpected exception: exit 0 silently (a shim hook must never break a shell); note this guard in the docstring.

Explicit decision to record: timeout/default-empty-→-YES applies only to a typed empty line (Enter); a TIMEOUT is "not now" — never auto-spawn N tabs at an unattended prompt.

- [ ] **Step 1: Write the failing tests** (`tests/test_cli.py`; monkeypatch `cli.rescue`, `cli.ops.detmux`, ttys, and `cli.select.select`):

```python
def test_rescue_check_silent_when_marker_exists(...):
    # marker written for current boot -> exit 0, no output, no detmux calls
def test_rescue_check_silent_when_not_a_tty(...):
    # rescued sessions exist but stdin.isatty() False -> silent, NO marker written
    # (a later interactive shell should still get the prompt)
def test_rescue_check_headless_prints_notice_once(...):
    # tab spawner None -> notice line, marker written; second call silent
def test_rescue_check_yes_opens_tabs_and_marks(...):
    # select returns readable, stdin line "y\n" -> detmux called per pid, marker written
def test_rescue_check_timeout_declines(...):
    # select returns ([], [], []) -> "not now" printed, NO detmux, marker written
```

- [ ] **Step 2: Run, watch each fail for the right reason.**
- [ ] **Step 3: Implement** `_cmd_rescue_check` per the decided behavior (import `select`; reuse `_tab_spawner`, `mutation_lock`, `ops.detmux` wiring from `_cmd_detmux`; wrap the whole body in `try/except Exception: return 0` with the shim-safety docstring). Register subparser with help `"[shim] once per boot, offer to re-home rescued conversations"`.
- [ ] **Step 4: Docs** — DESIGN.md paragraph (where the repair-loop/session-ops are described): the prompt set is "prior-boot entries parked in live tmux", once per boot via marker, timeout defaults to *not now*, headless degrades to a notice. ROADMAP Phase 3 bullet: mark restore-prompt UX built (unit-tested; live-verified pending). CHANGELOG Added entry.
- [ ] **Step 5: Full gates; commit** — `feat(rescue): interactive once-per-boot restore prompt (crr rescue-check)`.

---

### Task 3: Shim integration (fish/bash/zsh)

**Files:**
- Modify: `crr/shims/crr.fish`, `crr/shims/crr.bash`, `crr/shims/crr.zsh`
- Test: `tests/test_shims.py`

**Placement:** immediately after the `register` call in each shim. Interactive-gated, stdout NOT silenced (the prompt must reach the user), stderr silenced, absolute path, no-op when crr is missing (the `_crr`-style guard already handles absence — but `_crr` silences stdout in bash/zsh/fish helpers, so call the binary directly with its own guard):

fish:
```fish
# Phase-3 restore prompt: once per boot, offer to re-home rescued sessions.
# stdout stays attached (the [Y/n] prompt); stderr never leaks into the prompt.
if status is-interactive; and test -x "$_CRR_BIN"
    "$_CRR_BIN" rescue-check 2>/dev/null
end
```

bash:
```bash
# Phase-3 restore prompt: once per boot, offer to re-home rescued sessions.
if [[ $- == *i* && -x "$_CRR_BIN" ]]; then
    "$_CRR_BIN" rescue-check 2>/dev/null
fi
```

zsh:
```zsh
# Phase-3 restore prompt: once per boot, offer to re-home rescued sessions.
if [[ -o interactive && -x "$_CRR_BIN" ]]; then
    "$_CRR_BIN" rescue-check 2>/dev/null
fi
```

(Adapt variable names to what each shim actually uses — read them first; bash/zsh use `_CRR_BIN` analogues per their files.)

- [ ] **Step 1: Write the failing tests** in `tests/test_shims.py`, following its existing per-shell gating and fake-crr technique:
  - Contract test per shell (ungated, string-level): the shim text contains a `rescue-check` invocation guarded by that shell's interactive test and `-x` check, with `2>/dev/null` and WITHOUT `>/dev/null` on stdout.
  - Behavior test (per installed shell, mirroring existing fake-crr tests): an interactive shell sourcing the shim with a fake `crr` script logs a `rescue-check` call; a NON-interactive shell does not.
- [ ] **Step 2: Run, watch fail.** Fish is installed here; bash tests run; zsh tests skip (not installed — expected).
- [ ] **Step 3: Implement the three shim edits.**
- [ ] **Step 4: Full gates; commit** — `feat(shims): interactive shells run the once-per-boot restore prompt`.

---

### Task 4: Docs site (static, dependency-free)

**Files:**
- Create: `docs/site/index.html`, `docs/site/style.css`, `docs/site/dashboard.png` (copy of `hedylamarr-dashboard.png` from repo root)
- Modify: `README.md` (link the site), `ROADMAP.md` (Phase 5 docs-site bullet: built, publish pending), `CHANGELOG.md`
- Test: `tests/test_docs_site.py` (new)

**Content requirements (source material is in-repo; do not invent claims):**
- Hero: name, one-line mission (from DESIGN.md), the **"Not affiliated with Anthropic"** disclaimer (licensing requirement — DESIGN.md "Licensing / naming"), MIT license note, GitHub link.
- "How it works": journal → classifier (live/ghost/crashed) → reviver (detached tmux) → dashboard, summarized from DESIGN.md with the lesson-driven tone kept short.
- Install: pipx instructions + shim setup (`crr shim fish|bash|zsh`), service install (`crr systemd --install` / launchd / schtasks) — cross-check the exact commands against README.md.
- Commands: the README table, reproduced as an HTML table.
- Security model: summarized from SECURITY.md/DESIGN.md (loopback-only bind, tailnet as auth boundary, Host allowlist, JSON-POST CSRF gate, textContent-only DOM).
- Screenshot: `dashboard.png` with an honest caption.
- Calibration line in the footer (project rule: never overstate verification): "Linux/WSL is live-verified; macOS/Windows adapters are unit-tested, hardware verification pending."

**Form:** hand-written HTML5 + one small CSS file. NO external requests (no CDN, no webfonts, no analytics), works file:// and via GitHub Pages. Semantic tags, dark-scheme-aware via `prefers-color-scheme`. No JavaScript (nothing needs it; keeps the node gate irrelevant).

- [ ] **Step 1: Write the failing test** `tests/test_docs_site.py`:

```python
from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "docs" / "site"

class _Checker(HTMLParser):
    def __init__(self):
        super().__init__(); self.external = []
    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in ("src", "href") and v and v.startswith(("http://", "https://")) \
               and "github.com" not in v:
                self.external.append(v)

def test_site_exists_parses_and_is_self_contained():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    checker = _Checker(); checker.feed(html)
    assert checker.external == []            # no CDN/webfonts/analytics
    assert "Not affiliated with Anthropic" in html
    assert (SITE / "style.css").is_file()
    assert (SITE / "dashboard.png").is_file()

def test_site_commands_match_cli_surface():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for cmd in ("crr status", "crr reopen", "crr kick", "crr close", "crr dismiss",
                "crr detmux", "crr rescued", "crr doctor", "crr systemd"):
        assert cmd in html
```

- [ ] **Step 2: Run, watch fail (missing files).**
- [ ] **Step 3: Build the site** per the content requirements; copy the screenshot with `cp hedylamarr-dashboard.png docs/site/dashboard.png`.
- [ ] **Step 4: README link ("Docs: docs/site/ — servable via GitHub Pages"), ROADMAP + CHANGELOG updates.**
- [ ] **Step 5: Full gates; commit** — `feat(docs): static dependency-free docs site under docs/site/`.

---

### Task 5: WSL tab-spawn binaries in the service PATH (live bug, 2026-07-31)

**Files:**
- Modify: `crr/adapters/systemd.py` (`resolve_service_path` gains `extra_binaries`), `crr/cli.py` (`_cmd_systemd` passes `("wt.exe", "wsl.exe")` when `host.is_wsl()`), `crr/core/ops.py` (`_open_tab` honesty), `CHANGELOG.md`
- Test: `tests/test_systemd.py`, `tests/test_cli.py`, `tests/test_ops.py`

**Live evidence:** interactive shell resolves `wt.exe` (`/mnt/c/Users/Infin/AppData/Local/Microsoft/WindowsApps/wt.exe`) and `wsl.exe` (`/mnt/c/windows/system32/wsl.exe`); the deployed `crr-web.service` PATH contains neither → `WindowsTerminalSpawner.available()` is False inside the service → dashboard De-tmux refuses ("no terminal tab spawner is available on this host") and Reopen silently skips its tab. `[lesson: interop PATH]` recurring: the service PATH must resolve every binary the service calls, and tab spawning calls wt.exe/wsl.exe.

- [ ] **Step 1: Failing tests.** `tests/test_systemd.py`: `resolve_service_path(crr_bin, extra_binaries=("wt.exe",))` with a fake `shutil.which` mapping includes the extra binary's dir in the PATH string, and an unresolvable extra lands in `missing`. `tests/test_cli.py`: with `cli.host.is_wsl` monkeypatched True and a fake which, `crr systemd` (print mode) bakes the wt.exe dir into the emitted unit PATH; with `is_wsl` False, wt.exe is not consulted. `tests/test_ops.py`: `_open_tab(None, name)` (and an unavailable spawner) returns a message suffix containing `tmux attach -t <name>` instead of `""` — Reopen's response must say why no tab appeared.
- [ ] **Step 2: Watch them fail** (TypeError on the new kwarg; empty-string suffix).
- [ ] **Step 3: Implement.** `resolve_service_path(crr_bin, extra_binaries=())`: iterate `(*SERVICE_BINARIES, *extra_binaries)` in the existing loop. `_cmd_systemd`: `extras = ("wt.exe", "wsl.exe") if host.is_wsl() else ()`; pass through. `_open_tab`: unavailable spawner → `f" (no tab spawner on this host — attach with: tmux attach -t {name})"`. CHANGELOG Fixed entry.
- [ ] **Step 4: Full gates; commit** — `fix(systemd): bake WSL tab-spawn binaries (wt.exe/wsl.exe) into the service PATH`.
- [ ] **Step 5 (controller, post-merge): redeploy** — `crr systemd --install` + restart `crr-web.service`, then verify the service PATH resolves wt.exe and the dashboard De-tmux/Reopen actually spawn a tab.

---

### Task 6: Honest button labels + a real Un-tmux (user request 2026-07-31)

**Files:**
- Modify: `crr/core/page.html` (rename De-tmux button label to `Untrack`; add `Un-tmux` button), `crr/core/web.py` (`ACTIONS` += `"untmux"`; `PAGE_VERSION` 11 → 12), `crr/core/ops.py` (new `untmux` op), `crr/core/ports.py` (`TmuxSpawner.kill_session`), `crr/adapters/tmux.py` (`RealTmux.kill_session`), `crr/core/contracts.py` (`ARCHIVE_REASONS` += `"untmuxed"`, no version bump — same vocabulary-extension rationale as `ghost-restored`), `crr/cli.py` (`untmux` subcommand + web action branch), `CHANGELOG.md`, `README.md` (row)
- Test: `tests/test_ops.py`, `tests/test_web.py`, `tests/test_adapters.py` (tmux builder), `tests/test_contracts.py`, `tests/test_cli.py`

**Why:** The user reports the `De-tmux` label is dishonest: clicking it opens a tab that still runs tmux (it re-homes + untracks; it never removes the wrapper). Rename the label to `Untrack` (op name/API stays `detmux` — no API break). And add the genuinely-de-tmuxing op the label implied: `untmux`.

**`ops.untmux(store, archive, tmux, boot, probe, pid, now, *, tab_spawner)` (decided design):**
1. Same gates as `detmux` in the same order: entry read → classify == CRASHED → `tmux_session` set → name in `tmux.list_sessions()` → `tab_spawner` available (refuse BEFORE any destructive step; a missing spawner must not kill the tmux).
2. `tmux.kill_session(name)` — the parked claude dies; the conversation is durable in its transcript.
3. Spawn the visible tab running `["claude", "--resume", sid]` word-form with `cwd=entry["cwd"]` (`tab_spawner.open_tab(revival_argv(entry), cwd=entry["cwd"])`).
4. On spawn success: archive reason `"untmuxed"`, `store.remove(pid)`, message `f"un-tmuxed {pid}: claude --resume in a new tab; crr no longer manages it"`.
5. On spawn failure AFTER the kill: leave the journal entry untouched (its `tmux_session` field remains; the reviver's next pass re-parks the conversation in tmux — say so in the failure message: `"...tab failed to open: {exc}; the watchdog will re-park it in tmux within a minute"`). Return `ok=False`.
- `RealTmux.kill_session(name)`: `["tmux", "kill-session", "-t", name]`, `check=True`, timeout — pure builder `_kill_session_cmd(name)` + thin wrapper, mirroring `new_detached_session`.
- Web: `ACTIONS` += `"untmux"`; action branch mirrors detmux's wiring (boot/probe under the lock). CLI: `crr untmux <pid>` mirroring `_cmd_detmux`.
- Page (crashed cards): `Untrack` (op `detmux`) and, same condition, `addBtn("Un-tmux", "untmux", true)` — confirm-gated (second click) since it kills and relaunches. `PAGE_VERSION = 12  # v12: De-tmux renamed Untrack; real Un-tmux button`.

- [ ] **Step 1: failing tests** — ops: happy path (kill called, tab spawned with revival argv + cwd, archived `"untmuxed"`, delisted); spawner-missing refusal BEFORE kill (kill_session not called); spawn-failure path (entry retained, ok False, message mentions watchdog); live-session refusal (classifier gate). web: `"untmux"` in ACTIONS; unknown-op regression unchanged. contracts: `"untmuxed"` valid reason, version still 1. adapters: `_kill_session_cmd`. cli: subcommand parses. page: node gate + PAGE_VERSION 12 pin; `Untrack` label string present, `De-tmux` absent.
- [ ] **Step 2: watch each fail for the right reason.**
- [ ] **Step 3: implement.**
- [ ] **Step 4: full gates; commit** — `feat(untmux): real un-tmux op + honest Untrack label (PAGE_VERSION 12)`.

---

### Task 7: linger failure is a warning, not an install failure (WSL calibration)

**Files:**
- Modify: `crr/cli.py` (`_cmd_systemd` install branch), `CHANGELOG.md`
- Test: `tests/test_cli.py`

**Why (live evidence 2026-07-31):** on WSL2, `loginctl enable-linger` reliably exits 1 (dbus quirk; services run fine because the user manager starts with the session). Task 2's exit-code fix now makes `crr systemd --install` report total failure ("the watchdog/dashboard are NOT running") when everything except linger succeeded — an over-claim in the other direction.

**Decided behavior:** split the enable commands: run `daemon-reload` + the two `enable --now` commands via `_run_commands` (failure of ANY of these is still a hard failure, exit 1); run `loginctl enable-linger` separately — on nonzero exit print exactly one stderr warning: `crr systemd: warning — could not enable linger (common on WSL2); services will stop at logout unless linger is enabled another way` and do NOT fail the install. `systemd.enable_commands()` keeps returning all four (print-mode output unchanged); add `systemd.critical_enable_commands()` and `systemd.linger_command()` accessors so the split lives in the adapter, not as cli-side list slicing.

- [ ] **Step 1: failing tests** — linger-only failure → exit 0, success line printed, warning on stderr; enable-command failure → still exit 1, no success line; print mode unchanged (all four commands listed).
- [ ] **Step 2: watch fail** (current code exits 1 on linger failure).
- [ ] **Step 3: implement; Step 4: full gates; commit** — `fix(systemd): linger failure warns instead of failing the install (WSL2 quirk)`.

---

### Task 8: Atomic prompt claim + doc wording refresh (Task-3 review findings)

**Files:**
- Modify: `crr/core/rescue.py` (atomic claim), `crr/cli.py` (`_cmd_rescue_check` uses it), `CHANGELOG.md` + `DESIGN.md` (drop the now-stale "shim wiring pending a later task" phrasing — Task 3 landed it)
- Test: `tests/test_rescue.py`, `tests/test_cli.py`

**Why:** Task-3 review: `already_prompted()`/`mark_prompted()` is check-then-act — two interactive shells starting together (terminal app restoring several tabs) can BOTH prompt and BOTH detmux the same sessions. Fix with an atomic claim.

**Decided design:** add `rescue.claim_prompt(state_dir, boot_id) -> bool` — attempts `os.open(marker_path, O_CREAT | O_EXCL)`; True = this process won the claim (marker now exists; do the stale-marker sweep here too), False (`FileExistsError`) = another shell already claimed. `_cmd_rescue_check` flow changes: after the tty gate and rescued-sessions check, call `claim_prompt` INSTEAD of `already_prompted` + later `mark_prompted` — the winner claims BEFORE prompting (a mid-prompt Ctrl-C/kill no longer re-prompts next shell; `crr rescued` remains the recovery path), losers exit silently. The not-a-tty early-exit still happens BEFORE any claim (unchanged semantics: a non-tty shell never consumes the prompt). `already_prompted` stays for the fast-path pre-check (cheap exists() before scanning the journal) and tests; `mark_prompted` becomes unused by cli — remove it and its direct tests, moving the stale-sweep coverage onto `claim_prompt` (update Task-1's tests accordingly).

- [ ] **Step 1: failing tests** — `claim_prompt` returns True once and False for the second caller (two sequential calls; plus a threaded race test: N threads, exactly one True); winner-claims-before-prompt pinned in test_cli (simulate prompt raising after claim → marker still exists, rc 0); stale sweep moved to claim path; doc greps: CHANGELOG/DESIGN no longer say wiring is pending.
- [ ] **Step 2: watch fail. Step 3: implement. Step 4: full gates; commit** — `fix(rescue): atomic once-per-boot prompt claim (close the two-shell race)`.

---

## Out of scope (record for the report)

- Publishing the site (enabling GitHub Pages) and any announcement — human-gated per the runbook.
- macOS/Windows live verification of the prompt (fish/bash covered by tests here; zsh tests skip where zsh is absent).
- Auto-attaching without asking, or prompting in non-interactive shells — deliberately excluded (decisions recorded in Task 2).

## Self-review notes

- Task 2 consumes Task 1's exact `rescue.*` names; Task 3 consumes Task 2's command name `rescue-check`; Task 4 lists `crr rescued` in its command table (exists after Task 1).
- The one new config key lands in Task 1 with its test; Task 2 reads it via `config.get`.
- No stored/served contract shapes change anywhere; no PAGE_VERSION bump (page.html untouched).
- The marker's silent-tty rule: NOT writing the marker when not-a-tty is deliberate (a later interactive shell should still offer); pinned by `test_rescue_check_silent_when_not_a_tty`.
