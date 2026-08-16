# Smoother headless Linux — reopen restored conversations into tmux windows

**Status:** design · 2026-08-15
**Issue:** follow-on to #30 (the after-reboot restore prompt)
**Scope:** the headless path only — no GUI tab spawner. Desktop/WSL/macOS-with-GUI are untouched.

---

## Why

On a headless Linux box (a server: no `$DISPLAY`/`$WAYLAND_DISPLAY`, reached over SSH), crr has no GUI terminal to open a tab into. So the tab-opening features degrade to *printing* `tmux attach -t <name>` and leaving the user to run it by hand, one conversation at a time. After a reboot that means: SSH in, read a notice, then `tmux attach` per session, or hunt.

But a headless user is **always at a terminal**, and the restored conversations already live in individual `crr-<sid>` tmux sessions. The terminal-native equivalent of "open them all in tabs" is **tmux windows**. This spec makes crr present the restored conversations as windows and put the user in them — the same end goal as the GUI-tab experience, for a terminal.

Two pieces:
- **A** — the #30 restore prompt, on headless, gathers the restored conversations into tmux windows and drops the user in (instead of only printing a notice).
- **B** — a single `crr reopen <pid>` from an interactive SSH shell actually drops the user into that session (instead of only warning "no tab opened").

## Activation

Everything here activates only when BOTH:
- **headless** — the selected tab spawner is `None` (`_tab_spawner(config)` returns `(None, ...)`, i.e. no `$DISPLAY`/`$WAYLAND_DISPLAY` and/or no known terminal), and
- **a tty is present** — `sys.stdin.isatty() and sys.stdout.isatty()`.

If a GUI tab spawner exists (desktop/WSL), nothing here runs — the existing GUI-tab path is unchanged. If there is no tty (a script, a non-interactive context), the existing one-line notice is unchanged.

## The shared primitive: "reopen into this terminal"

Both A and B reduce to one move, parameterized by the restored session names, whether the caller is inside tmux (`$TMUX` set), and whether a tty is present:

- **In tmux** (`$TMUX` set): for each restored session, `rename-window -t crr-<sid>:0 <cwd-basename>` (renaming the *source* window — it's the shared window, so the readable name shows in every session it's linked into, and the target is unambiguous without tracking result indices), then `link-window -s crr-<sid>:0 -t <current_session>` into the caller's **current** session. Print `linked N restored conversation(s) here — Ctrl-b w to list`. **No forced switch** — the user keeps their place and moves when ready.
- **Not in tmux, tty present, N > 1:** (re)build an aggregate session named `crr-restored` — `kill-session -t crr-restored` first if it already exists (stale from a prior detach; safe — its windows survive in their `crr-<sid>` sessions), `new-detached-session crr-restored` (creates a placeholder window 0), `rename-window` + `link-window -s crr-<sid>:0 -t crr-restored` each restored window in, `kill-window -t crr-restored:0` to drop the placeholder shell — then **`exec tmux attach -t crr-restored`**. The caller's shell blocks in tmux until the user detaches, then resumes at its prompt.
- **Not in tmux, tty present, N == 1:** `exec tmux attach -t crr-<sid>` directly (no aggregate wrapper for a single conversation).
- **No tty:** unchanged — print the one-line notice with `tmux attach -t <name>`.

`link-window` **shares** a window between sessions rather than moving it: each restored conversation stays in its own tracked `crr-<sid>` session AND appears in the aggregate/current session. So nothing is untracked, and detaching kills nothing — consistent with the #30/#33 "keep it tracked" principle. Killing/rebuilding the `crr-restored` aggregate never destroys a conversation, because each linked window still lives in its `crr-<sid>` session (a tmux window is only destroyed when its LAST linking session drops it).

## Where A and B plug in

**A — the restore prompt (`_rescue_check`).** Today the headless branch is: no tab spawner → print a one-line notice, return (never prompt). New: on headless **with a tty**, prompt `[Y/n]` exactly like the GUI path, and on `[Y]` run the shared primitive over the whole restored set. The once-per-boot marker claim, the tty gate, and timeout/Ctrl-C = "not now" are all unchanged. The headless-**without-tty** case keeps the existing notice (there is no one to prompt).

**B — single reopen (`_cmd_reopen`).** Today, after `ops.reopen` succeeds but reports `degraded` (no tab), the CLI prints a "no tab opened" warning. New: when headless **with a tty**, run the shared primitive on the one reopened session instead of the warning — attach if not in tmux, link-as-window if in tmux. `ops.reopen`'s own behavior (attach a tab, keep tracked) is unchanged; this only changes what the CLI does when the session is alive but no GUI tab could be opened.

## Architecture (fits crr's one-way layering)

- **Core (pure, exhaustively testable, no tmux):**
  `plan_terminal_reopen(session_names, *, in_tmux, has_tty, current_session, aggregate="crr-restored") -> TerminalReopenPlan`
  returns a frozen plan describing what to do: an ordered list of tmux command argvs to run (link/rename/kill/new-session), plus an optional `exec_argv` (the `tmux attach …` to exec), plus a human `message`. It performs NO I/O — it only decides. Every branch above is a pure function of its inputs.
  Lives in a small new module `crr/core/terminal_reopen.py` (one responsibility), or alongside `crr.core.rescue` if that stays cohesive.
- **Adapters:** new `TmuxSpawner` methods for the verbs the plan needs — `link_window(src, dst)`, `rename_window(target, name)`, `kill_window(target)`, and `current_session_name()` (`tmux display-message -p '#S'`, used by the cli to resolve `current_session` when in tmux); reuse `new_detached_session`/`kill_session`. Each is a thin subprocess wrapper with a pure command-builder (`_link_window_cmd`, …), matching the existing tmux adapter style. The plan runner can also just execute the plan's argvs generically via one `run(argv)` path rather than a method per verb — either is fine as long as command shapes are pure/testable.
- **cli (composition + the two side effects core must not do):** resolves `in_tmux` from `os.environ.get("TMUX")`, `has_tty` from the isatty checks, and — when in tmux — `current_session` via the adapter's `current_session_name()`; calls `plan_terminal_reopen`; runs the plan's tmux commands via the adapter; and performs the `exec_argv` via an **injected exec seam** — a module-level `_exec = os.execvp` indirection (mirroring harden's injected `_run_commands`) so tests substitute a recorder and **no test ever attaches or execs a real process**. The link/rename/kill commands run under the existing `mutation_lock`; the lock context is **exited before the `exec`** — an `exec` inherits the process's open fds, so execing while holding the lock fd would keep the journal mutation lock held for the entire tmux attach. Order: acquire lock → run the plan's tmux commands → release lock → `exec_argv` (if any).

`crr.core.terminal_reopen` imports nothing from adapters/cli (pure). The exec seam and `$TMUX`/tty reads live in cli. Layering (`cli → adapters → core`) holds.

## Security / safety

- No new network surface, no web terminal, no stored secrets. `crr-<sid>` names are metacharacter-free (`crr-<uuid>`), safe to render into tmux argvs.
- The `exec`/attach is gated behind a tty check and only reached on an explicit `[Y]` or `crr reopen`; it is never taken non-interactively.
- HARD TEST RULE: no test attaches real tmux, links real windows, or execs. Command generation is asserted as argvs; the runner and the exec are injected fakes that record calls. The real attach/exec only happens on the user's live interactive action.

## Testing

- **Core `plan_terminal_reopen` — every branch:** in-tmux (link+rename each into `current_session`, no exec, message names Ctrl-b w); not-in-tmux N>1 (kill-if-exists + new-session + link each + kill placeholder + `exec_argv == ["tmux","attach","-t","crr-restored"]`); not-in-tmux N==1 (`exec_argv == ["tmux","attach","-t","crr-<sid>"]`, no aggregate); no-tty (no commands, no exec, notice message). Empty session set → empty plan (no-op), never an exec.
- **Adapter command builders:** `_link_window_cmd`, `_rename_window_cmd`, `_kill_window_cmd` asserted as exact argvs (word-form; pure, no server), matching the existing `_new_session_cmd`/`_kill_session_cmd` test pattern.
- **cli A (`rescue-check`) headless+tty:** with a fake tmux (list/attached sets), a headless `_tab_spawner` (None), a monkeypatched `$TMUX`, and an injected exec-recorder, `[Y]` produces the expected link/attach sequence; the recorder captures the `tmux attach` instead of running it. In-tmux variant asserts link-into-current + no exec. The once-per-boot marker still guards a second call; `[n]`/timeout does nothing.
- **cli B (`crr reopen`) headless+tty:** a degraded (no-tab) reopen on a parked session drives the same primitive — attach recorded (not in tmux) or link recorded (in tmux) — replacing the old warning. Desktop (`_tab_spawner` present) still opens a GUI tab, no attach.
- **Regression:** desktop/WSL GUI-tab paths and the no-tty headless notice are unchanged (existing tests stay green).

## Out of scope

- The dashboard "Reopen" on a headless server — it has no tty to attach the user's SSH terminal, so it keeps doing its durable `ops.reopen` and showing the `tmux attach` hint. No change.
- A browser/web terminal (ttyd-style) — a large, security-sensitive new surface against crr's stdlib-only ethos. Not now.
- macOS/Windows — unaffected; this is the headless-Linux (no-GUI) path.
- Changing `ops.reopen`'s core behavior — it already attaches-and-keeps-tracked; this spec only changes the CLI's *headless fallback* presentation.
