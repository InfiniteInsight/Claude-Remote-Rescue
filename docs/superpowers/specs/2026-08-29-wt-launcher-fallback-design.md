# Windows Terminal Launcher Fallback

**Supersedes** `2026-08-27-doctor-wt-alias-guidance-design.md`, which made
repair guidance the deliverable. This one makes the alias *optional*: when
the `wt.exe` App Execution Alias is unusable, CRR opens the tab anyway.
Guidance drops to a hedged footnote.

## Goal

A broken Windows Terminal App Execution Alias should stop being an outage.
CRR resolves a working launcher at spawn time, falling through tiers until
one opens a window, and only reports a problem when every tier fails.

## Why this instead of guidance

The previous spec's deliverable was a doctor line telling the user how to
re-enable the alias by hand. Three problems killed it:

1. **The diagnosis is not trustworthy.** `wt_probe` only knows that
   `wt.exe --version` did not succeed. The adapter's own comment says that
   catches "broken App Execution Aliases **AND** contexts where wt.exe
   cannot exec (tmux, systemd)." Telling a user to go toggle a Settings
   switch that is actually fine — because a systemd-hosted service happened
   to be the caller — is worse than saying nothing.
2. **Guidance does not restore service.** Even a correct diagnosis leaves
   tabs broken until a human is physically at the Windows machine.
3. **Auto-repair is out of the question.** There is no supported API to
   toggle an alias; the real repairs (Appx re-registration, undocumented
   registry state) are heavyweight and sometimes elevation-gated, and CRR
   would be reaching out of WSL to mutate Windows app registration as a
   side effect of opening a terminal tab. Not for a session-rescue tool —
   especially on top of a diagnosis it cannot trust (point 1).

Bypassing is strictly better: it fixes the user's actual problem (no tab)
without touching their system.

## Key finding that makes this cheap

The alias is only a launcher shim. Verified on this host:

- The alias stub is `/mnt/c/Users/<u>/AppData/Local/Microsoft/WindowsApps/wt.exe`
  — **2 bytes**, a reparse point. This is the thing that breaks.
- The **real** `wt.exe` (132,920 bytes) is present and readable at
  `C:\Program Files\WindowsApps\Microsoft.WindowsTerminal_<version>_x64__8wekyb3d8bbwe\wt.exe`,
  alongside `WindowsTerminal.exe`. `Get-AppxPackage` reports
  `Status: Ok`.
- `cmd.exe`, `powershell.exe`, `conhost.exe`, `explorer.exe`, and `wsl.exe`
  are all reachable from WSL.

So the fallback is not a degraded imitation — it is *the same binary with
the same arguments*, reached by a different path. Tab behavior is identical.

**Unverified, and the plan must verify it:** readability does not guarantee
executability under the `WindowsApps` ACLs. Tier 2 must be proven to
actually launch before it is trusted; if execution is blocked, Tier 3
carries the fallback instead.

## Design: tiered launcher resolution

`crr/adapters/tab_spawn_windows.py` resolves a launcher at spawn time and
falls through on failure. Tiers are attempted **in-line during a real spawn**
— never as a speculative pre-probe — so no tier costs a GUI window.

| Tier | Launcher | Result |
|---|---|---|
| 1 | `wt.exe` from PATH (the alias stub) — today's behavior | a WT tab |
| 2 | real `wt.exe` inside the WindowsApps package dir | a WT tab, identical |
| 3 | plain console: `cmd.exe /c start "" wsl.exe --distribution <d> -e <argv>` | a visible window, not a WT tab |
| — | all failed | report unavailable (today's behavior) |

Tier 2 resolution: glob
`/mnt/*/Program Files/WindowsApps/Microsoft.WindowsTerminal_*_x64__*/wt.exe`,
choosing the highest version when several are installed. This mirrors the
existing `wt_path()` fallback (which globs the *stub* location, and
therefore cannot help when the stub itself is the broken thing).

Tier 3 is deliberately last: it opens a standalone console window rather
than a tab in the user's existing terminal. Functional, less pretty. It
exists so that a totally absent or unusable Windows Terminal still yields a
visible, attachable session.

### Why falling through is cheap

When the alias is disabled, executing the stub fails immediately with an
exec error — it does not hang and does not open a window. So trying the
next tier inside the same `open_tab` call costs microseconds and no UI.
This is why no new probe is needed and why `wt_probe`'s window-popping
behavior stays confined to where it already lives.

### What does not change

- `wt_probe`, and the existing probe/no-probe policy across destructive vs.
  best-effort spawn paths (`available(probe=...)`) — untouched.
- The `TabSpawnTimeout` contract: a cold Windows Terminal that outruns the
  budget is still "could not confirm," never "failed" (#53). A timeout does
  **not** trigger fallthrough — the tab may well have opened, and launching
  a second window would be worse than waiting.
- The word-form argv contract: every tier takes argv directly, no shell
  string.

## Health reporting (the demoted footnote)

Running on a fallback tier forever is a quietly degraded state, so it is
worth surfacing — as information, not an alarm.

A small store records which tier last succeeded, written only from spawn
attempts that already happen (no new probes). `crr doctor` reads it:

- Tier 1 → `[ok] Windows Terminal tab spawn — wt.exe`
- Tier 2 → `[ok] Windows Terminal tab spawn — via the app package (the
  wt.exe alias appears unusable; tabs are opening normally)`, plus the
  one-line note below
- Tier 3 → `[ok] tab spawn — console fallback (Windows Terminal
  unavailable; tabs open in a separate window)`
- all tiers failed → `[warn]` naming the last error
- no record → `[ok] tab spawn — not yet exercised`

The alias note, shown only for Tier 2 and worded to survive a wrong guess:

```
Tabs are opening through the app package rather than the wt.exe alias.
If you want the alias back: Settings -> Apps -> Advanced app settings ->
App execution aliases -> turn on "Terminal (wt.exe)". Nothing is broken
in crr either way.
```

No claim that the alias *is* disabled — only that CRR is not using it.
That is the honest statement, and it is immune to the misattribution
problem that sank the previous spec.

Doctor shows the record's timestamp, since it reports history rather than a
live test.

## Architecture

One-way layering holds: `crr.cli` → `crr.adapters` → `crr.core`.

- **`crr/core/tab_health.py`** (new, pure): `TabHealthStore(state_dir)` over
  a versioned `tab_health.json` — atomic write, degrade-to-None on
  missing/corrupt/wrong-version, mirroring `settings.py`/`exclusions.py`.
  Records `{tier, detail, ts, boot_id}`. `TAB_HEALTH_STORE_VERSION = 1` in
  `contracts.py`. Also holds the pure doctor-line formatting so it is
  testable without Windows.
- **`crr/adapters/tab_spawn_windows.py`**: `package_wt_path()` (Tier 2
  resolution) and `console_command()` (Tier 3 argv builder); `open_tab`
  falls through the tiers and reports which one succeeded. The adapter
  classifies and returns; it never writes state.
- **`crr/cli.py`**: records the reported tier at the spawn sites that
  already exist, and renders the doctor line through the existing `_check`
  renderer.

## Testing

- **Tier resolution** (pure/unit): `package_wt_path()` picks the highest
  version from a faked `/mnt/c/Program Files/WindowsApps` tree; returns
  None when absent. `console_command()` builds correct word-form argv with
  and without a distro.
- **Fallthrough** (`open_tab` with a faked subprocess runner): Tier 1
  exec-error → Tier 2 attempted; Tier 2 exec-error → Tier 3 attempted;
  first success stops the chain and reports its tier; all-fail reports
  unavailable. **A `TabSpawnTimeout` at any tier stops the chain** and does
  not fall through.
- **Store**: record/read round-trip, missing → None, corrupt → None, wrong
  version → None. `tmp_path` only.
- **Doctor rendering**: each tier's line, the Tier 2 alias note, the
  no-record line; non-WSL hosts omit the section.
- No test launches a real window, runs a real `wt.exe`, writes outside
  `tmp_path`, or reaches an unstubbed `cli._exec` (the conftest autouse
  guard enforces the last).
- **Manual verification required before merge** (cannot be done from Linux
  CI): confirm Tier 2 actually *executes* from the WindowsApps path on a
  real host, and confirm Tier 3 opens a usable window. If Tier 2 proves
  non-executable, it is dropped and Tier 3 becomes the sole fallback.

## Global Constraints

- Zero runtime dependencies (stdlib only).
- One-way layering; decisions in core, I/O in adapters, wiring in cli.
- TDD: tests first, implementation second.
- `TAB_HEALTH_STORE_VERSION = 1` added with the store-version cluster in
  `contracts.py`.
- No `page.html` change, so no `PAGE_VERSION` bump; console output only —
  the dashboard diagnostics payload and `DIAGNOSTICS_CONTRACT` are
  untouched.
