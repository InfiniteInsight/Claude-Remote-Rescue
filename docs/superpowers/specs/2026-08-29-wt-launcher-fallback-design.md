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

1. **The diagnosis is not trustworthy.** `wt_probe` only knows that
   `wt.exe --version` did not succeed. The adapter's own comment says that
   catches "broken App Execution Aliases **AND** contexts where wt.exe
   cannot exec (tmux, systemd)." Sending a user to toggle a Settings switch
   that is fine is worse than saying nothing.
2. **Guidance does not restore service.** Even a correct diagnosis leaves
   tabs broken until a human is at the Windows machine.
3. **Auto-repair is out of the question.** No supported API toggles an
   alias; the real repairs (Appx re-registration, undocumented registry
   state) are heavyweight and sometimes elevation-gated, and CRR would be
   reaching out of WSL to mutate Windows app registration as a side effect
   of opening a tab — on top of a diagnosis it cannot trust.

Bypassing fixes the user's actual problem (no tab) without touching their
system.

## Verified launcher matrix

Measured on this host (WSL Ubuntu-24.04, Windows Terminal
`Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe`, `Status: Ok`)
on 2026-08-29. **Every row was executed, not assumed.**

| Route | Result |
|---|---|
| `wt.exe` from PATH (the alias stub) | **works** (exit 0) — the alias is healthy on this host today |
| Direct exec of the real `wt.exe` in `C:\Program Files\WindowsApps\…\` | **BLOCKED** — exit 126, `Permission denied` |
| `Start-Process 'shell:appsFolder\Microsoft.WindowsTerminal_8wekyb3d8bbwe!App' -ArgumentList new-tab,…` | **works** — a real WT tab, arguments pass through, alias bypassed |
| `cmd.exe /c start "" wsl.exe -e …` | **fails** — no execution, even from a Windows-visible cwd |
| `Start-Process wsl.exe -ArgumentList '-e',…` | **works** — plain console window |

Two corrections to the earlier draft, both from measurement:

- The real `wt.exe` inside the package is **readable but not executable**
  from WSL — the `WindowsApps` ACLs block execution. The previous draft
  proposed using it directly; that tier is dead and is dropped.
- The shell **AUMID** route replaces it and is strictly better: it keys off
  the package *family* name (`Microsoft.WindowsTerminal_8wekyb3d8bbwe`),
  which is stable across Windows Terminal versions, so there is no version
  glob to maintain and no path to go stale on upgrade.

The alias stub itself is a 2-byte reparse point (`MZ`) — that is the shim
that breaks, which is why a stub-location fallback like the existing
`wt_path()` glob cannot rescue this case.

## Design: tiered launcher resolution

`crr/adapters/tab_spawn_windows.py` resolves a launcher at spawn time and
falls through on failure. Tiers are attempted **in-line during a real
spawn** — never as a speculative pre-probe — so no tier costs a GUI window.

| Tier | Launcher | Result |
|---|---|---|
| 1 | `wt.exe` from PATH (alias stub) — today's behavior | a WT tab |
| 2 | `powershell.exe Start-Process 'shell:appsFolder\Microsoft.WindowsTerminal_8wekyb3d8bbwe!App' -ArgumentList new-tab,…` | a WT tab, alias bypassed |
| 3 | `powershell.exe Start-Process wsl.exe -ArgumentList '--distribution',<d>,'-e',<argv…>` | a visible console window, not a WT tab |
| — | all failed | report unavailable (today's behavior) |

Tier 3 is deliberately last: a standalone console window rather than a tab
in the user's terminal. Functional, less pretty. It exists so a totally
absent or unusable Windows Terminal still yields a visible, attachable
session.

### Why falling through is cheap

When the alias is disabled, executing the stub fails immediately with an
exec error — it does not hang and does not open a window. Trying the next
tier inside the same `open_tab` call therefore costs milliseconds and no
UI.

### Two costs Tiers 2 and 3 introduce (and how to handle them)

1. **PowerShell startup latency.** Both fallback tiers pay
   `powershell.exe` startup (hundreds of ms, occasionally more on a cold
   host). This eats into the tab-spawn budget. The plan must confirm the
   fallback path still fits the existing timeout contract, and must not let
   a slow fallback be reported as a failure — `TabSpawnTimeout` still means
   "could not confirm," never "failed" (#53).
2. **`Start-Process` is fire-and-forget.** It returns as soon as the
   process is launched, so unlike `wt.exe new-tab` there is no meaningful
   exit code proving the tab actually opened. Tiers 2 and 3 can therefore
   report "launched" but not "confirmed." The plan must decide this
   explicitly and keep it honest — a launch that cannot be confirmed is
   reported as unconfirmed, matching how #182 already treats a Reopen that
   delivers no tab. **Do not let a fire-and-forget launch masquerade as a
   verified success.**

### What does not change

- `wt_probe`, and the probe/no-probe policy across destructive vs.
  best-effort spawn paths (`available(probe=...)`) — untouched.
- The `TabSpawnTimeout` contract (#53).
- The word-form argv contract: every tier takes argv directly, no shell
  string. Tier 2/3 build a PowerShell `-ArgumentList` array; quoting for
  paths with spaces is the adapter's job and must be unit-tested.

## Health reporting (the demoted footnote)

Running on a fallback tier forever is a quietly degraded state, worth
surfacing as information, not an alarm.

A small store records which tier last succeeded, written only from spawn
attempts that already happen (no new probes). `crr doctor` reads it:

- Tier 1 → `[ok] Windows Terminal tab spawn — wt.exe`
- Tier 2 → `[ok] Windows Terminal tab spawn — via the app package (the
  wt.exe alias appears unusable; tabs are opening normally)`, plus the note
  below
- Tier 3 → `[ok] tab spawn — console fallback (Windows Terminal
  unavailable; tabs open in a separate window)`
- all tiers failed → `[warn]` naming the last error
- no record → `[ok] tab spawn — not yet exercised`

The alias note, shown only for Tier 2, worded to survive a wrong guess:

```
Tabs are opening through the app package rather than the wt.exe alias.
If you want the alias back: Settings -> Apps -> Advanced app settings ->
App execution aliases -> turn on "Terminal (wt.exe)". Nothing is broken
in crr either way.
```

No claim that the alias *is* disabled — only that CRR is not using it. That
is honest and immune to the misattribution problem that sank the previous
spec. Doctor shows the record's timestamp, since it reports history rather
than a live test.

## Architecture

One-way layering holds: `crr.cli` → `crr.adapters` → `crr.core`.

- **`crr/core/tab_health.py`** (new, pure): `TabHealthStore(state_dir)` over
  a versioned `tab_health.json` — atomic write, degrade-to-None on
  missing/corrupt/wrong-version, mirroring `settings.py`/`exclusions.py`.
  Records `{tier, detail, ts, boot_id}`. `TAB_HEALTH_STORE_VERSION = 1` in
  `contracts.py`. Also holds the pure doctor-line formatting, so it is
  testable without Windows.
- **`crr/adapters/tab_spawn_windows.py`**: `aumid_command()` (Tier 2 argv
  builder) and `console_command()` (Tier 3 argv builder); `open_tab` falls
  through the tiers and reports which one succeeded and whether it was
  confirmed. The adapter classifies and returns; it never writes state.
- **`crr/cli.py`**: records the reported tier at the spawn sites that
  already exist, and renders the doctor line through the existing `_check`
  renderer.

## Testing

- **Argv builders** (pure/unit): `aumid_command()` produces the AUMID and a
  correct `-ArgumentList` array; `console_command()` builds correct
  word-form argv with and without a distro; both quote paths containing
  spaces correctly.
- **Fallthrough** (`open_tab` with a faked subprocess runner): Tier 1
  exec-error → Tier 2 attempted; Tier 2 failure → Tier 3 attempted; first
  success stops the chain and reports its tier; all-fail reports
  unavailable. **A `TabSpawnTimeout` at any tier stops the chain** and does
  not fall through (the tab may have opened; a second window is worse).
- **Confirmation honesty**: a Tier 2/3 launch reports "launched,
  unconfirmed" and never a confirmed success.
- **Store**: record/read round-trip; missing → None; corrupt → None; wrong
  version → None. `tmp_path` only.
- **Doctor rendering**: each tier's line, the Tier 2 alias note, the
  no-record line; non-WSL hosts omit the section.
- No test launches a real window, runs a real `wt.exe`/`powershell.exe`,
  writes outside `tmp_path`, or reaches an unstubbed `cli._exec` (the
  conftest autouse guard enforces the last).
- **Manual verification on a real host before merge**: with the alias
  deliberately disabled, confirm Tier 2 opens a working tab and the doctor
  line reads correctly. The launcher matrix above is already verified; this
  confirms the wired-up fallthrough.

## Global Constraints

- Zero runtime dependencies (stdlib only).
- One-way layering; decisions in core, I/O in adapters, wiring in cli.
- TDD: tests first, implementation second.
- `TAB_HEALTH_STORE_VERSION = 1` added with the store-version cluster in
  `contracts.py`.
- No `page.html` change, so no `PAGE_VERSION` bump; console output only —
  the dashboard diagnostics payload and `DIAGNOSTICS_CONTRACT` are
  untouched.
