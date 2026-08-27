# Doctor Guidance for a Broken Windows Terminal wt.exe Alias

## Goal

Make `crr doctor` tell a WSL user when Windows Terminal tab-spawning is
failing because the `wt.exe` **App Execution Alias** is disabled, and print
the exact manual steps to repair it — without ever popping the GUI window
that the authoritative check (`wt.exe --version`) causes.

## Motivation

CRR runs inside WSL and opens "visible tabs" via `wt.exe new-tab`. When the
Windows Terminal App Execution Alias is toggled off, a 0-byte stub remains
on PATH: the windowless checks (`wt_path()` finds it, `interop_registered()`
confirms the binfmt handler) both pass, yet executing it fails. Detection
and graceful degradation already shipped (`wt_probe()` +
`WindowsTerminalSpawner.available(probe=...)`, commit `a334446` and the
tab-spawn cluster): CRR routes around the broken alias and reports a failed
tab open instead of losing the session. What is missing is the **repair
guidance** — nothing tells the user *how* to fix the alias. This closes
that gap (task #92) for every WSL user, not just the author's box.

## Design Constraint: no gratuitous GUI window

`wt.exe --version` (`wt_probe`) is the only authoritative alias test and it
opens a Windows Terminal window, because wt is a GUI app with no console.
The existing code deliberately runs it only before *destructive* spawns
(untmux/detmux), never on best-effort paths. `doctor` must honor that:
it must not run the probe. Therefore doctor cannot test the alias live — it
reports the **cached outcome of the last real spawn attempt** instead.

## Architecture

One-way layering holds: `crr.cli` → `crr.adapters` → `crr.core`. The new
store is pure core; the adapter only classifies (never persists); the CLI
orchestrates recording and rendering.

### 1. New core store — `crr/core/tab_health.py`

`TabHealthStore(state_dir)`, mirroring the existing store pattern
(`settings.py`, `exclusions.py`): versioned JSON file `tab_health.json` in
the state dir, atomic write via `write_json_atomic`, degrade-to-None on a
missing/corrupt/wrong-version file.

API:

- `record(status: str, detail: str = "", *, now: str, boot_id: str) -> None`
- `read() -> dict | None` — the last record, or None if absent/corrupt.

Record shape:

```json
{
  "v": 1,
  "status": "alias_broken",
  "detail": "wt.exe --version returned non-zero",
  "ts": "2026-08-27T15:04:00Z",
  "boot_id": "conftest-boot-id-0000"
}
```

`status` is one of: `ok`, `alias_broken`, `no_wt`, `no_interop`,
`spawn_failed`. `TAB_HEALTH_STORE_VERSION = 1` is added to `contracts.py`
next to the other store versions; `store_version_ok` gates reads.

### 2. Adapter classification — `tab_spawn_windows.py`

Add `WindowsTerminalSpawner.classify() -> str` returning the finer verdict
from the checks the adapter already performs, in order:

1. `wt_path()` is None → `no_wt`
2. `interop_registered()` is False → `no_interop`
3. `wt_probe(path, timeout)` is False (wt + interop both present) →
   `alias_broken`
4. otherwise → `ok`

`available()` is unchanged (still returns bool for the spawn gate);
`classify()` simply exposes the reason. The adapter never writes state and
never imports the store — it stays a pure classifier. `classify()` runs the
probe, so callers invoke it only where a probe is already acceptable (see
§3); doctor never calls it.

### 3. CLI records health at existing spawn points — `crr/cli.py`

Health is recorded only from spawn attempts that **already happen** — no new
probe is introduced:

- **Destructive-spawn gate (untmux/detmux):** these already call
  `available(probe=True)`. Replace/augment with `classify()` (same probe,
  finer result) and write it: `ok`, `alias_broken`, `no_wt`, or
  `no_interop`.
- **Successful `open_tab`:** record `ok`.
- **`open_tab` raising a non-timeout error:** record `spawn_failed` (weaker
  signal; doctor phrases it cautiously).
- **`TabSpawnTimeout`:** record nothing — a cold wt can outrun the budget
  and still open the tab (#53); crying "broken" here would be a lie.

Recording uses `TabHealthStore(sd).record(status, detail, now=<iso>,
boot_id=<detected>)`. `now` and `boot_id` come from the same sources the
rest of cli already uses, so tests inject them.

### 4. Doctor renders the cached health — `_cmd_doctor`

A new check in the WSL / tab-spawn section, via the shared `_check`
renderer (tri-state ok), reading `TabHealthStore(sd).read()`:

| Cached status | Doctor line |
|---|---|
| (no record) | `[ok] Windows Terminal tab spawn — not yet exercised` |
| `ok` | `[ok] Windows Terminal tab spawn — last open succeeded (<ts>)` |
| `alias_broken` | `[warn] Windows Terminal tab spawn failed — the wt.exe App Execution Alias looks disabled (last attempt <ts>)` + repair block |
| `no_wt` | `[warn] wt.exe not found — tab reopen degraded (last attempt <ts>)` + short pointer |
| `no_interop` | `[warn] WSL interop handler missing — wt.exe cannot exec (last attempt <ts>)` + one-line pointer to re-register binfmt |
| `spawn_failed` | `[warn] Windows Terminal tab spawn failed to open a window (last attempt <ts>) — if tabs aren't opening, the alias may be disabled` + repair block |

The `no_interop` case gets its own concise line here (doctor stays
self-contained) pointing at the binfmt re-registration; it does not attempt
to fix it. Only the WSL host shows this section; on non-WSL hosts the check
is omitted, matching how doctor already gates platform-specific lines.

**Repair block (printed for `alias_broken` and `spawn_failed`):**

```
Repair the wt.exe App Execution Alias (on Windows):
  1. Settings -> Apps -> Advanced app settings -> App execution aliases
     (Windows 10: Apps -> Apps & features -> App execution aliases)
  2. Find "Terminal (wt.exe)" and turn it On. If already On, toggle Off then On.
  3. If it's missing entirely, repair Windows Terminal: Settings -> Apps ->
     Windows Terminal -> Advanced options -> Repair (or reinstall via the
     Microsoft Store / `winget install Microsoft.WindowsTerminal`).
  4. Verify from WSL:  wt.exe --version   (should print a version)
```

## Staleness

The cached record is historical: the alias may have been fixed since. Doctor
always shows the record's timestamp ("last attempt <ts>") so a warning reads
as a past event, not a live probe. A record from a previous boot is still
shown (dated by its timestamp); the user re-tests by triggering a Reopen,
which refreshes the record. No expiry logic — a timestamped line is honest
enough, and silent expiry would hide a still-broken alias.

## What does NOT change

- `wt_probe`, `available()`, and the probe/no-probe policy across spawn
  paths — untouched.
- The dashboard `/api/diagnostics` payload and `DIAGNOSTICS_CONTRACT` —
  untouched. This is `crr doctor` console output only (YAGNI: no dashboard
  surfacing until asked).
- No `PAGE_VERSION`, no config key.

## Contract changes

- `TAB_HEALTH_STORE_VERSION = 1` in `contracts.py`.

## Testing

- **Core store** (`tests/test_tab_health.py`): record→read round-trip;
  missing file → None; corrupt JSON → None; wrong version → None; atomic
  write leaves no partial file. `tmp_path` only.
- **Adapter `classify()`** (extend `tests/test_tab_spawn_windows.py` or a
  sibling): each branch — `no_wt` (wt_path None), `no_interop`
  (interop False), `alias_broken` (wt+interop present, probe False), `ok`
  (probe True) — by monkeypatching `wt_path`, `interop_registered`,
  `wt_probe`. No real subprocess, no window.
- **CLI recording**: a destructive-spawn path with a stubbed spawner whose
  `classify()` returns each status writes the matching record; a
  `TabSpawnTimeout` writes nothing. State dir is `tmp_path`; the `_exec`
  seam and any real spawn are stubbed (the conftest guard is active).
- **Doctor rendering**: with a seeded `tab_health.json`, `crr doctor` prints
  the right line and — for `alias_broken`/`spawn_failed` — the repair block;
  with no file, the neutral "not yet exercised" line; a non-WSL host omits
  the section. Assertions match on stable substrings (status phrase, `<ts>`,
  a repair-step anchor like "App execution aliases").

## Global Constraints

- Zero runtime dependencies (stdlib only).
- One-way layering: `tab_health.py` is pure core; the adapter classifies but
  never persists; only `crr.cli` wires the store to the spawner and doctor.
- TDD: tests first, implementation second.
- No test runs a real `wt.exe`, spawns a real window, or reaches an
  unstubbed `_exec` (the conftest autouse guard enforces the last).
- Version ledger: `TAB_HEALTH_STORE_VERSION` added with the store-version
  cluster in `contracts.py`.
