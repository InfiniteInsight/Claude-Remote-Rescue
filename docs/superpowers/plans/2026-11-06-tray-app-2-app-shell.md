# Tray app, part 2: the app shell (tray, window, Control tab, Sessions tab)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Windows tray app in `tray/` that shows crr's state in the tray, starts and stops crr, shows the phone QR code, and shows the dashboard itself in a Sessions tab. It never wakes WSL on its own.

**Architecture:**
- **Shape.** An Electron app (TypeScript, esbuild, vitest, electron-builder), modelled on the Qwen Remote Launcher. Its tested modules are copied in and adapted.
- **Main process.** It owns the tray, the window, all `wsl.exe` calls and a sequential status poller. It talks to crr only through the Part 1 commands (`crr service status|start|stop`, `crr status --json`, `crr qr --json`, `crr systemd --install`), run as argv over `wsl.exe -d <distro> -e <crr path>`.
- **Control tab.** The app's own sandboxed renderer, driven by a pure view-model.
- **Sessions tab.** A `WebContentsView` that loads the loopback dashboard in a persistent partition, with no preload.

**Tech Stack:** Electron 32, TypeScript 5, esbuild 0.23, vitest 2, electron-builder 24 (Windows x64 `dir` target), and `qrcode` as the only runtime dependency. Node 22 is used for tooling.

**Spec:** `docs/superpowers/specs/2026-11-05-tray-app-design.md`: "The app (`tray/`)", "What it looks like", "Constraints", "Anti-goals", and suggested-order step 2. Logs and Settings tabs (step 3), tab handoff (step 4) and dashboard additions (step 5) have their own plans.

## Global Constraints

- **crr stays dependency-free.** Its runtime dependencies stay at zero. `pyproject.toml` doesn't change, and nothing under `crr/` imports or references `tray/`.
- **Reuse the Qwen launcher's code** (owner's decision). The source is `~/projects/qwen-code/packages/launcher-app` at commit `dee185032a`.
  - A copied file keeps its `@license` header ("Copyright 2025 Qwen Team", `SPDX-License-Identifier: Apache-2.0`). Add the line ` * Modified for Claude-Remote-Rescue (crr), 2026.` directly under the SPDX line.
  - New files carry no Qwen header. They're MIT, like the repo.
  - `tray/LICENSE-APACHE` holds the Apache-2.0 text, and `tray/NOTICE` names the source and commit.
- **How the app reaches crr.** Only two ways:
  - HTTP to the dashboard on `127.0.0.1` at the port `crr service status --json` reports;
  - `wsl.exe -d <distro> -e <absolute crr path> <args…>`, built as an argv array, never a shell string.
  - The single exception is the fixed path-resolution script in Task 3, which is a constant, not interpolated.
- **No PATH inside `wsl.exe -e`.** The crr path is resolved once (Task 3) and stored in the app's config.
- **Never wake WSL.**
  - Before any `wsl.exe -d` call, the poller checks `wsl.exe -l --running -q` and crr's Windows-side hold flag (`C:\ProgramData\crr\holder-hold.json`).
  - If the distro isn't running, or a hold is in place, the poller makes no `-d` call.
  - Only an explicit user action boots WSL: Start CRR, Open dashboard in browser, Repair, or choosing a distro.
- **Electron security.**
  - The app's renderer runs with `contextIsolation: true`, `sandbox: true` and `nodeIntegration: false`, with a preload exposing `window.crr`.
  - The Sessions view has no preload, partition `persist:crr-dashboard`, context isolation and sandbox on, and navigation locked to the dashboard origin. Other http(s) links open in the default browser.
  - Permission requests from the dashboard page are denied.
- **No secrets in the app.** The app never handles the dashboard passphrase or its cookie. It stores no secrets: its config holds only the distro, the crr path and the window bounds. It writes no crr state or config file.
- **Closing the window hides it to the tray.** Only the tray menu's Quit ends the app.
- **Tray menu**, top to bottom: a status line; Open CRR; Open dashboard in browser; Start CRR or Stop CRR; Start at login (packaged builds only); Quit tray app.
- **Tray icon states:**
  - running;
  - needs attention: a session is crashed, running elsewhere, or in conflict (two agents);
  - stopped or unreachable;
  - WSL down.
  - Keep-awake alone being stopped doesn't count as stopped.
- **Contracts the app reads** (crr's JSON is its interface):
  - `service` v1 and `qr` v1 exactly;
  - `crr status --json` with `contract >= 19`, reading only `sessions[].state`, `.running_elsewhere` and `.conflict`.
  - Anything else is a clear "crr version mismatch" state, never a crash.
- **Windows only.** Keep code platform-neutral where it's cheap.
- **Tests.** Pure logic gets vitest tests, run in CI on ubuntu. Electron runtime behaviour is covered by `tray/OPERATOR-VERIFY.md`, run by the owner on Windows.
- **Commits** end with the session's attribution trailer. The repo's pre-commit hook runs the Python suite, so set `PYTEST_ADDOPTS="--deselect tests/test_e2e_linux.py::test_register_launch_crash_revive_status_web"` (host-only failure, #176) and `PYTHONPATH=$PWD` when committing from a worktree.

## Review Focus

1. **WSL shut down while the app runs** (`wsl --shutdown`, or WSL idling out). The app must not boot it back. That means no `wsl.exe -d` call until the distro shows up in `wsl.exe -l --running -q` again. Owned by Task 5, `test_no_distro_call_while_the_distro_is_stopped`.
2. **A crr hold in place while WSL is still up.** `crr holder pause` or `stop` lets WSL go down, and a 5-second poll would keep it alive. The app must make no `-d` call while `holds()` is true. Owned by Task 4 (parity with `crr.core.holder_hold.holds`) and Task 5, `test_no_distro_call_while_a_hold_is_in_place`.
3. **crr answers in a way the app doesn't expect.** Examples: an older crr without `service`, a newer contract version, non-JSON on stdout, a non-zero exit, `wsl.exe` missing or timing out. The app shows a clear state and keeps polling, never crashing. Owned by Task 3, `test_unknown_contract_version_is_a_mismatch_not_a_crash` and `test_non_json_stdout_is_an_error`.
4. **Navigation away from the dashboard inside the Sessions tab.** Cases: a link to another site, a redirect, `javascript:` or `file:` URLs, and a dashboard port change. Same-origin navigation stays, other http(s) goes to the browser, and everything else is denied. Owned by Task 8, `test_navigation_decision`.
5. **Slow `wsl.exe`** (about 1.4 s measured per call) **and system sleep.** Polls never overlap. A manual refresh during a poll joins it rather than starting a second one, and polling pauses on suspend. Owned by Task 5, `test_polls_never_overlap` and `test_pause_stops_scheduling`.

---

### Task 1: The `tray/` package, licence files, and the CI job

**Files:**
- Create:
  - `tray/package.json`, `tray/package-lock.json`, `tray/tsconfig.json`, `tray/vitest.config.ts`, `tray/esbuild.js`, `tray/electron-builder.yml`
  - `tray/LICENSE-APACHE`, `tray/NOTICE`, `tray/README.md`
  - `tray/src/main/buildInfo.ts` and `tray/src/main/buildInfo.test.ts`, copied from Qwen
- Modify: `.gitignore` (add `tray/node_modules/` and `tray/release/`; `dist/` is already ignored repo-wide) and `.github/workflows/ci.yml` (new `tray` job)

**Interfaces:**
- Produces:
  - npm scripts `build` (`node esbuild.js`), `start` (`npm run build && electron .`), `test` (`vitest run`), `typecheck` (`tsc --noEmit`) and `package:win` (`node esbuild.js --production && electron-builder --win --x64`)
  - `buildStamp()` and `trayTooltip()` from `buildInfo.ts`, unchanged from Qwen apart from the product name

- [ ] **Step 1: Create the package.**
  - `package.json`: name `crr-tray`, `private: true`, `type: module`, `main: dist/main/main.cjs`.
    - Dependencies: `qrcode ^1.5.3`.
    - devDependencies: `electron ^32.0.0`, `electron-builder ^24.13.3`, `esbuild ^0.23.0`, `typescript ^5.5.0`, `vitest ^2.0.0`, `@types/node ^20.0.0`, `@types/qrcode ^1.5.5`. The Qwen package got typescript and vitest from its monorepo root; this one must declare them.
  - `tsconfig.json` and `vitest.config.ts`: copy from Qwen. Those two files have no header.
  - `esbuild.js`: copy from Qwen, keeping the build-stamp `define`. Its entry points are `src/main/main.ts`, `src/preload/preload.ts` and `src/renderer/renderer.ts`, which Task 7 creates. Until then `npm run build` isn't run in CI.
  - `electron-builder.yml`: copy from Qwen, with `appId: 'io.github.infiniteinsight.crr-tray'` and `productName: 'CRR'`, and with the `win.icon` line removed. The exe keeps Electron's default icon; icon art is left to the builder.
  - Run `npm install` to produce the lockfile.
- [ ] **Step 2: Licence files.**
  - `LICENSE-APACHE` is the Apache-2.0 text, copied from the Qwen repo's root `LICENSE`.
  - `NOTICE` reads: `crr-tray includes software from qwen-code's packages/launcher-app (https://github.com/InfiniteInsight/qwen-code, commit dee185032a), Copyright 2025 Qwen Team, licensed under the Apache License 2.0 (see LICENSE-APACHE). Files carrying the Qwen Team header were copied and modified for crr.`
- [ ] **Step 3: Copy `buildInfo.ts` and `buildInfo.test.ts`.** Add the Modified-for-crr line, and change the product name in `trayTooltip` to `CRR`. Run `cd tray && npx vitest run`. Expected: the copied tests pass.
- [ ] **Step 4: Add the CI job `tray`.**
  - It runs on `ubuntu-latest` with `actions/setup-node@v4` and `node-version: 22`.
  - `defaults.run.working-directory: tray`.
  - `env: ELECTRON_SKIP_BINARY_DOWNLOAD: 1`.
  - Steps: `npm ci`, `npm run typecheck`, `npm test`.
- [ ] **Step 5: `tray/README.md`.** One short page: what the app is, `npm install`, `npm start` (from Windows), `npm run package:win` (from WSL with wine, as the Qwen launcher is built), and copying `release/win-unpacked` to Windows.
- [ ] **Step 6: Commit** with `feat: tray/ package for the crr tray app, reusing the Qwen launcher (#161)`.

### Task 2: `wsl.ts`: argv-only `wsl.exe` calls and distro discovery

**Files:**
- Create: `tray/src/main/wsl.ts`, copied from Qwen's `wsl.ts` and adapted; `tray/src/main/wsl.test.ts`

**Interfaces:**
- Produces:
  - `interface CommandResult { code: number; stdout: string; stderr: string }`
  - `type RunArgv = (argv: readonly string[]) => Promise<CommandResult>`
  - `type ExecFile = (file: string, args: readonly string[], opts: { encoding: 'utf8' | 'buffer'; timeout: number; maxBuffer: number }, cb: (err: Error | null, stdout: string | Buffer, stderr: string | Buffer) => void) => void`
  - `WSL_TIMEOUT_MS = 20_000`
  - `realRunInDistro(distro: string, exec?: ExecFile): RunArgv`. It runs `wsl.exe` with args `['-d', distro, '-e', ...argv]` and no shell. It never rejects. A timeout resolves `{ code: -1, stdout, stderr: 'timed out' }`, and a missing `wsl.exe` resolves `{ code: -1, stderr: 'wsl.exe not found' }`.
  - `decodeWslOutput(buf: Buffer): string`, which decodes UTF-16LE when the buffer contains a NUL byte, and UTF-8 otherwise
  - `parseDistroList(raw: string): string[]`, kept from Qwen
  - `parseDefaultDistro(raw: string): string | undefined`, which returns the line `wsl.exe -l` marks `(Default)`
  - `listDistros(exec?): Promise<string[]>` (`-l -q`), `defaultDistro(exec?): Promise<string | undefined>` (`-l`) and `runningDistros(exec?): Promise<string[] | undefined>` (`-l --running -q`).
    - `runningDistros` returns `undefined` only when `wsl.exe` can't be run at all.
    - A non-zero exit, which is what "There are no running distributions" produces, returns `[]`.

- [ ] **Step 1: Write the failing tests.**
  - `test_run_in_distro_builds_argv_without_a_shell`: a fake exec records `('wsl.exe', ['-d','Ubuntu-24.04','-e','/x/crr','service','status','--json'])`, and the options have no `shell` key.
  - `test_run_in_distro_never_rejects`: the fake errors with `code: 'ENOENT'`, which resolves code -1 and `'wsl.exe not found'`. A fake timeout (`killed: true`) resolves `'timed out'`.
  - `test_decode_wsl_output`: `Buffer.from('Ubuntu\r\n', 'utf16le')` decodes to `'Ubuntu\r\n'`, and plain UTF-8 passes through unchanged.
  - `test_parse_default_distro`: `'Windows Subsystem for Linux Distributions:\r\nUbuntu-24.04 (Default)\r\ndocker-desktop\r\n'` gives `'Ubuntu-24.04'`.
  - `test_running_distros_empty_on_nonzero_exit` and `test_running_distros_undefined_without_wsl`.
- [ ] **Step 2:** Run `npx vitest run src/main/wsl.test.ts`. Expected: FAIL, because the module is missing.
- [ ] **Step 3: Implement**, adapting Qwen's `realRunWsl`: argv instead of `bash -lc`, plus the injected exec.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: argv-only wsl.exe calls for the tray app (#161)`.

### Task 3: crr's contracts in TypeScript, and the crr client

**Files:**
- Create:
  - `tray/src/main/contracts.ts`, `tray/src/main/crrClient.ts`, `tray/src/main/contracts.test.ts`, `tray/src/main/crrClient.test.ts`
  - `tray/src/main/summarizeError.ts`, copied from Qwen's `appLifecycle.summarizeError` with its header
  - `tray/src/main/fixtures/` (JSON captured from a real `crr` 2bfb235: `service status --json`, `qr --json`, `status --json`)

**Interfaces:**
- Consumes: `RunArgv` and `CommandResult` from Task 2.
- Produces, in `contracts.ts` (pure):
  - `SERVICE_CONTRACT = 1`, `QR_CONTRACT = 1`, `SESSIONS_MIN_CONTRACT = 19`, `class ContractError extends Error`.
  - `interface ServiceUnit { name: 'watchdog' | 'dashboard' | 'keep-awake'; unit: string; installed: boolean; enabled: boolean; active: boolean }`
  - `interface ServiceStatus { contract: 1; backend: 'systemd' | 'unsupported'; reason: string; state: 'not-installed' | 'running' | 'stopped' | 'partial'; services: ServiceUnit[]; dashboard_url: string; dashboard_login: boolean }`
  - `interface QrInfo { contract: 1; provider: string | null; url: string | null; loopback_url: string; hint: string | null; dashboard_reachable: boolean; degraded: boolean }`
  - `interface SessionCounts { total: number; live: number; parked: number; crashed: number; ghost: number; attention: number }`. `attention` counts sessions where `state === 'crashed' || running_elsewhere || conflict`.
  - `parseServiceStatus(stdout: string): ServiceStatus`, `parseQr(stdout: string): QrInfo` and `parseSessionCounts(stdout: string): SessionCounts`. All three throw `ContractError`.
    - Exact keys apply for `service` and `qr`, mirroring `crr/core/contracts.py` `validate_service_payload` and `validate_qr_payload`.
    - For sessions, `contract` must be an int `>= 19` and `sessions` an array. Each state must be one of `live`, `ghost`, `crashed`, `parked`. `running_elsewhere` and `conflict` must be booleans. Other keys are ignored.
- Produces, in `crrClient.ts`:
  - `CRR_PATH_SCRIPT = 'printf "%s" "${XDG_STATE_HOME:-$HOME/.local/state}/crr/app/bin/crr"'`
  - `type CrrResult<T> = { ok: true; value: T } | { ok: false; error: string; exit: number; mismatch: boolean }`
  - `resolveCrrPath(run: RunArgv): Promise<{ ok: true; path: string } | { ok: false; error: string }>`. It runs `['sh', '-c', CRR_PATH_SCRIPT]`, then `[path, '--version']`, whose stdout must start with `crr `.
  - `class CrrClient` with `constructor(run: RunArgv, crrPath: string)` and these methods:
    - `serviceStatus(): Promise<CrrResult<ServiceStatus>>`
    - `sessionCounts(): Promise<CrrResult<SessionCounts>>`
    - `qr(): Promise<CrrResult<QrInfo>>`
    - `start(): Promise<CrrResult<string>>`, `stop(): Promise<CrrResult<string>>` and `repair(): Promise<CrrResult<string>>`. `repair` runs `crr systemd --install`. Each returns crr's stdout message on success, or its stderr on failure.
  - The error text is `summarizeError(stderr)` from `summarizeError.ts`: the last non-empty line, at most 90 chars, unchanged from Qwen.
  - `mismatch: true` when the cause is a `ContractError`, or a crr usage error (exit 2 with `invalid choice` or `unrecognized arguments` on stderr, meaning an older crr).

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_service_status_fixture`: the fixture parses, with `state` `'running'` and three units.
  - `test_unknown_contract_version_is_a_mismatch_not_a_crash`: the service fixture with `contract: 2` gives `{ ok: false, mismatch: true }`.
  - `test_non_json_stdout_is_an_error`: `'Traceback…'` gives `ok: false` and no throw.
  - `test_session_counts`: the status fixture gives totals matching the fixture's states. One session edited to `running_elsewhere: true` makes `attention === 1`.
  - `test_client_argv`: a fake run records `[path,'service','status','--json']`, `[path,'status','--json']`, `[path,'qr','--json']`, `[path,'service','start']`, `[path,'service','stop']` and `[path,'systemd','--install']`.
  - `test_stop_refusal_surfaces_crrs_message`: exit 1 with stderr ending `run crr systemd --install first` gives `error` equal to that last line.
  - `test_old_crr_is_a_mismatch`: exit 2 with `invalid choice: 'service'` gives `mismatch: true`.
  - `test_resolve_crr_path`: the version check failing gives `ok: false` with an error naming the path.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3: Implement both modules.** Capture the fixtures with the deployed crr (`~/.local/state/crr/app/bin/crr … > fixtures/….json`). Strip session titles and prompts from the status fixture down to the fields the parser reads, plus `session_id`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: the tray app reads crr's service, qr and sessions contracts (#161)`.

### Task 4: `hold.ts`: crr's Windows-side hold flag, read without touching WSL

**Files:**
- Create: `tray/src/main/hold.ts`, `tray/src/main/hold.test.ts`

**Interfaces:**
- Produces:
  - `HOLD_FILE = 'C:\\ProgramData\\crr\\holder-hold.json'`, `HOLDER_HOLD_STORE_VERSION = 1`, `BOOT_MATCH_TOLERANCE_SECONDS = 5`
  - `holds(record: unknown, nowSec: number, machineBootSec: number | undefined): boolean`. This mirrors `crr/core/holder_hold.py` `holds()`:
    - an invalid record means false (fail open);
    - `stop` means true;
    - `pause` means `now < until`;
    - `until-reboot` means true unless the boot clearly differs; an unknown boot means true.
    - Validity mirrors `crr/core/contracts.py` `validate_holder_hold`: `v === 1`, a valid mode, exact keys per mode, and integer non-boolean fields.
  - `readHold(path?: string, read?: (p: string) => string): unknown`, which returns `undefined` on any error
  - `machineBootSeconds(nowMs?: number, uptimeSec?: number): number`, equal to `nowMs / 1000 - uptimeSec`, with defaults from `Date.now()` and `os.uptime()`

- [ ] **Step 1: Write the failing test `test_holds_matches_crr`**, a table:
  - `undefined` gives false;
  - `{v:1,mode:'stop',set_at:1}` gives true;
  - `pause` with `until` in the future gives true, and in the past gives false;
  - `until-reboot` with `boot` 3 s from the machine boot gives true, 600 s away gives false, and an unknown boot (`undefined`) gives true;
  - `v:2` gives false;
  - extra key `x:1` gives false;
  - `set_at: true` gives false;
  - a string record gives false.

  Also `test_read_hold_missing_file_is_undefined`.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run it. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: the tray app honours crr's holder hold without waking WSL (#161)`.

### Task 5: The poller, which turns crr's answers into a tray state and never wakes WSL

**Files:**
- Create: `tray/src/main/poll.ts` (new; replaces Qwen's `statusPoll.ts`), `tray/src/main/poll.test.ts`

**Interfaces:**
- Consumes: `CrrClient`, `CrrResult`, `ServiceStatus` and `SessionCounts` (Task 3); `holds`, `readHold` and `machineBootSeconds` (Task 4); `runningDistros` (Task 2).
- Produces:
  - `POLL_MS = 5_000`. That is the delay after a poll *finishes*, never an interval.
  - `type TrayIcon = 'running' | 'attention' | 'stopped' | 'wsl-down'`
  - `interface PollState { icon: TrayIcon; statusLine: string; service?: ServiceStatus; counts?: SessionCounts; error?: string; mismatch?: boolean; wsl: 'missing' | 'stopped' | 'held' | 'running' }`
  - `interface PollInput { running: string[] | undefined; distro: string | undefined; held: boolean; clientError?: string; service?: CrrResult<ServiceStatus>; sessions?: CrrResult<SessionCounts> }`
  - `reduce(input: PollInput): PollState`. The rules, in order:
    1. `running` undefined: `wsl-down`, `WSL is not available`.
    2. No distro, or the distro isn't in `running`: `wsl-down`, `WSL is stopped`.
    3. `held`: `wsl-down`, `crr holder is holding WSL down — not checking crr`.
    4. `clientError`: `stopped`, `crr not found in <distro>: <clientError>`.
    5. Service not ok: `stopped`, and either `crr version mismatch — update crr or the tray app` (when `mismatch`) or `CRR unreachable: <error>`.
    6. Backend unsupported: `stopped`, with `reason`.
    7. `not-installed`: `stopped`, `CRR is not installed`.
    8. The watchdog or dashboard unit is inactive: `stopped`, `CRR stopped`. Keep-awake is ignored here.
    9. Running: `attention` if `counts.attention > 0`, otherwise `running`. The status line is `CRR running · <total> sessions, <crashed> crashed`. Unknown counts give `CRR running · sessions unknown`.
  - `interface PollerDeps { runningDistros(): Promise<string[] | undefined>; distro(): string | undefined; held(): boolean; client(): Promise<CrrClient | { error: string }>; onState(s: PollState): void; schedule(fn: () => void, ms: number): () => void }`
    - `client()` is the *only* place the crr path gets resolved: main returns a cached client, or runs `resolveCrrPath` and saves the path. Because the poller calls it only after the running and hold checks pass, resolving the path can never boot WSL. A hidden launch at login therefore never wakes it.
  - `class Poller` with `constructor(deps: PollerDeps)` and these methods:
    - `pollNow(): Promise<PollState>`. It joins an in-flight poll instead of starting another.
    - `start(): void`
    - `pause(): void`. It cancels the next scheduled poll and stops rescheduling.
    - `resume(): void`
  - One poll goes: `runningDistros()`, then `held()`. Only if both allow does it call `client().serviceStatus()`, and then `sessionCounts()` only when the service is running.

- [ ] **Step 1: Write the failing tests.**
  - `test_reduce_rules`: a table with one row per rule above, asserting `icon` and `statusLine`. Include a row where only keep-awake is inactive, which still gives `running`.
  - `test_no_distro_call_while_the_distro_is_stopped`: `runningDistros` gives `['docker-desktop']`. The fake `client()` and its methods count calls, and both stay at 0.
  - `test_no_distro_call_while_a_hold_is_in_place`: `client()` is never called.
  - `test_client_error_is_reported`: `client()` resolves `{ error: 'no crr' }`, which gives `stopped` and `crr not found in Ubuntu-24.04: no crr`.
  - `test_polls_never_overlap`: `serviceStatus` resolves on a deferred promise. Two `pollNow()` calls return the same promise, and `serviceStatus` is called once.
  - `test_pause_stops_scheduling`: with a fake `schedule`, the cancel function is called on `pause()`, and nothing is scheduled after a poll completes while paused.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: the tray app polls crr without ever waking WSL (#161)`.

### Task 6: The tray icon, menu and lifecycle rules

**Files:**
- Create:
  - `tray/src/main/appLifecycle.ts` and `appLifecycle.test.ts`, copied from Qwen and adapted;
  - `tray/src/main/tray.ts`, copied from Qwen and adapted;
  - `tray/src/main/trayIcon.ts` and `trayIcon.test.ts`, new.

**Interfaces:**
- Consumes: `PollState` and `TrayIcon` (Task 5).
- Produces, in `appLifecycle.ts`:
  - Kept from Qwen: `LaunchFlags`, `LOGIN_ITEM_ARGS = ['--hidden']`, `parseLaunchFlags`, `shouldHideInsteadOfClose` and `shouldQuitOnWindowClosed`, with their tests.
  - Removed: `terminalDrainNote` and `TRAY_IDLE_NOTE`, which belong to tab handoff in Part 4, and `summarizeError`, which now lives in Task 3's `summarizeError.ts`.
  - `TRAY_LABELS = { open: 'Open CRR', dashboard: 'Open dashboard in browser', start: 'Start CRR', stop: 'Stop CRR', loginItem: 'Start at login', quit: 'Quit tray app' }`
  - `type MenuItemId = 'status' | 'open' | 'dashboard' | 'startStop' | 'loginItem' | 'quit'`
  - `trayMenuSpec(opts: { state: PollState; packaged: boolean; loginItemEnabled: boolean; busy: boolean }): Array<{ id: MenuItemId; label: string; enabled: boolean; checked?: boolean }>`. It returns the items in the Global Constraints order.
    - The `status` item is always disabled.
    - `startStop` reads Stop CRR when the dashboard and watchdog units are active, and Start CRR otherwise. It's disabled while `busy`, and when the backend is unsupported or the state is `not-installed`. It stays enabled while WSL is down, because Start is an explicit, WSL-booting action.
    - `dashboard` is enabled only when the dashboard unit is active.
    - `loginItem` appears only when `packaged`.
- Produces, in `trayIcon.ts`:
  - `TRAY_ICON_COLORS: Record<TrayIcon, [number, number, number]> = { running: [22,163,74], attention: [217,119,6], stopped: [107,114,128], 'wsl-down': [30,64,175] }`
  - `trayIconPng(icon: TrayIcon, size = 32): Buffer`: a filled circle on transparent, encoded as PNG with `node:zlib` `deflateSync` and a table-based CRC32. No image library.
- Produces, in `tray.ts`: `createTray(opts: { image: (icon: TrayIcon) => Electron.NativeImage; host: { show(): void; openDashboard(): void; startStop(): void; quit(): void }; loginItem?: { get(): boolean; set(v: boolean): void } }): { update(state: PollState, busy: boolean): void }`. It keeps Qwen's click-to-show behaviour and its keep-alive closure.

- [ ] **Step 1: Write the failing tests.**
  - `test_tray_menu_order_and_labels`: the item ids appear in the exact order, with labels from `TRAY_LABELS`.
  - `test_start_stop_label_and_enablement`: running gives Stop CRR; stopped gives Start CRR; wsl-down gives Start CRR enabled; not-installed gives it disabled; busy gives it disabled.
  - `test_login_item_only_when_packaged`.
  - `test_tray_icon_png`: the bytes start with the PNG signature; IHDR is 32×32 RGBA; after inflating IDAT, the centre pixel equals `TRAY_ICON_COLORS[icon]` with alpha 255 and the corner pixel has alpha 0; the four icons differ.
  - The kept Qwen tests for `parseLaunchFlags` and the close and quit rules.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: the tray icon and menu reflect crr's state (#161)`.

### Task 7: The window, app config, IPC, and the Control tab

**Files:**
- Create:
  - `tray/src/main/appConfig.ts` and `appConfig.test.ts`, copied from Qwen and adapted;
  - `tray/src/main/controlView.ts` and `controlView.test.ts`, new;
  - `tray/src/main/ipc.ts`, `tray/src/main/main.ts`, `tray/src/preload/preload.ts`, all following Qwen's structure;
  - `tray/src/renderer/index.html`, `renderer.ts` and `styles.css`.
- Modify: `.github/workflows/ci.yml` (add `npm run build` to the `tray` job).

**Interfaces:**
- Consumes: Tasks 2 to 6.
- Produces, in `appConfig.ts`:
  - `interface AppConfig { distro?: string; crrPath?: string; windowBounds?: WindowBounds }`
  - `parseAppConfig(raw: unknown): AppConfig` (pure, and drops unknown or mistyped fields), plus `readAppConfig()` and `writeAppConfig(c)`. The file is `<userData>/crr-tray.json`, and it holds no secrets.
- Produces, in `controlView.ts` (pure):
  - `type ControlView = { rows: Array<{ name: 'watchdog' | 'dashboard' | 'keep-awake'; label: string; dot: 'on' | 'off' | 'missing' }>; startStop: { label: string; enabled: boolean }; repair: { visible: boolean; enabled: boolean }; phone: { kind: 'qr'; url: string; image: string } | { kind: 'hint'; text: string } | { kind: 'none'; text: string }; login: 'on' | 'off' | 'unknown'; banner?: string; distros: string[]; distro?: string }`
  - Row labels: `Watchdog (revives sessions)`, `Dashboard`, `Keep-awake`.
  - `controlView(input: { state: PollState; qr?: CrrResult<QrInfo>; qrImage?: string; busy: boolean; lastAction?: { ok: boolean; message: string }; distros: string[]; distro?: string; crrPath?: { ok: boolean; error?: string } }): ControlView`
    - `repair.visible` is true only when the backend is `systemd` and the state is `not-installed`.
    - `phone` is `qr` when the QR call succeeded with a `url` and an image; `hint` with text `To reach it from your phone: <hint>` when `url` is null; `none` otherwise.
    - `banner` holds, in priority order: a failed `lastAction` message; the crr-path error (`crr not found in <distro>: <error>`); the `wsl-down` and `stopped` status lines; and `qr.degraded`, which appends `the tunnel answer may be out of date`.
- Produces, in `main.ts`: `mainWindowWebPreferences(preloadPath: string): Electron.WebPreferences`, returning `{ preload, contextIsolation: true, sandbox: true, nodeIntegration: false }`. `main.ts` also does the following:
  - The composition: single-instance lock, `--hidden` support, close-hides-to-tray and `before-quit`, as in Qwen.
  - `powerMonitor` `suspend`/`resume` call `poller.pause()`/`resume()`.
  - On first run it picks the distro: the saved value, else `defaultDistro()`. `wsl.exe -l` doesn't boot anything.
  - It implements `PollerDeps.client()` (Task 5): the cached crr path, else `resolveCrrPath`, saved per distro. A distro change clears the cache. Choosing a distro is an explicit action, so it calls `poller.pollNow()` after a single `realRunInDistro(distro)(['true'])`, which may boot it.
  - Start at login: when `app.isPackaged`, the tray's login item calls `app.setLoginItemSettings({ openAtLogin, args: LOGIN_ITEM_ARGS })` and reads `getLoginItemSettings().openAtLogin`, as Qwen does.
  - It calls `qr()` when the window is shown, then every 60 s while the Control tab is visible, and renders it with `QRCode.toDataURL(url)` in main.
  - Every `PollState` updates the tray and pushes a fresh `ControlView` to the renderer.
- Produces, in `preload.ts`: `window.crr = { onView(cb: (v: ControlView) => void): void; start(): Promise<void>; stop(): Promise<void>; repair(): Promise<void>; setDistro(d: string): Promise<void>; refresh(): Promise<void>; showTab(tab: 'control' | 'sessions'): Promise<void> }`.
- The renderer is two tabs, Control and Sessions. It's a dumb DOM render of `ControlView`, with a CSP of `default-src 'self'; img-src 'self' data:; style-src 'self'`.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_app_config`: unknown keys are dropped; a non-string `crrPath` is dropped; bounds need numeric width and height.
  - `test_control_view_rows_and_buttons`: a running fixture gives three `on` dots and Stop CRR; a not-installed fixture gives a visible Repair; an unsupported fixture gives no Repair, with the reason in the banner.
  - `test_control_view_phone`: a URL gives `qr`; a null URL with a hint gives the exact hint text; an error gives `none`.
  - `test_control_view_banner_priority`.
  - `test_main_window_is_sandboxed`, which checks `mainWindowWebPreferences`.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** Copy `appConfig.ts`, `ipc.ts`, `main.ts` and `preload.ts` from Qwen and cut them down: no `envConfig`, `launcherClient` or terminal drain. Write the renderer fresh.
- [ ] **Step 4:** Run `npm run typecheck && npm test && npm run build`. Expected: all pass, and `dist/main/main.cjs` exists. Add the build step to CI.
- [ ] **Step 5: Commit** with `feat: the tray app window and Control tab (#161)`.

### Task 8: The Sessions tab, which shows the dashboard itself

**Files:**
- Create: `tray/src/main/sessionsView.ts` and `tray/src/main/sessionsView.test.ts`. Modify: `tray/src/main/main.ts` and `tray/src/main/ipc.ts`, to show and hide the view on `showTab`.

**Interfaces:**
- Consumes: `PollState.service.dashboard_url` (Task 5), and `showTab` (Task 7).
- Produces:
  - `DASHBOARD_PARTITION = 'persist:crr-dashboard'`, `TAB_BAR_HEIGHT = 40`
  - `sessionsWebPreferences(): Electron.WebPreferences`, returning `{ partition: DASHBOARD_PARTITION, contextIsolation: true, sandbox: true, nodeIntegration: false }`. There is no `preload` key.
  - `dashboardOrigin(dashboardUrl: string): string | undefined`. It returns the URL's origin only for `http:` with host `127.0.0.1`, and `undefined` otherwise.
  - `navigationDecision(target: string, origin: string): 'allow' | 'external' | 'deny'`. Same origin means allow. Any other `http:` or `https:` means external. Every other scheme, or an unparsable URL, means deny.
  - `attachSessionsView(win: Electron.BrowserWindow, host: { openExternal(url: string): void }): { show(dashboardUrl: string | undefined): void; hide(): void }`. It works like this:
    - It creates the `WebContentsView` with `sessionsWebPreferences()`.
    - It wires `will-navigate`, `will-redirect` and `setWindowOpenHandler` through `navigationDecision`. New windows are always denied, and `external` calls `openExternal`.
    - It denies every permission request with `session.fromPartition(DASHBOARD_PARTITION).setPermissionRequestHandler((_wc, _p, cb) => cb(false))`.
    - It lays the view out below `TAB_BAR_HEIGHT` and follows window resizes.
    - When the origin changes, it reloads the new URL.
  - When the dashboard isn't active, `show()` hides the view, and the renderer's Sessions panel shows `CRR is stopped` with a Start CRR button that calls `window.crr.start()`.

- [ ] **Step 1: Write the failing tests.**
  - `test_navigation_decision`, a table against origin `http://127.0.0.1:8377`:
    - `http://127.0.0.1:8377/x?y` gives allow;
    - `http://127.0.0.1:9000/` gives external;
    - `https://github.com/` gives external;
    - `http://localhost:8377/` gives external, because it's a different origin;
    - `javascript:alert(1)`, `file:///C:/x`, `data:text/html,x` and `devtools://x` give deny;
    - `not a url` gives deny.
  - `test_dashboard_origin_is_loopback_only`: `http://127.0.0.1:8377/` gives the origin; `http://example.com:8377/` and `https://127.0.0.1:8377/` give undefined.
  - `test_sessions_view_has_no_preload`: `'preload' in sessionsWebPreferences()` is false, and the partition starts with `persist:`.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `npm run typecheck && npm test && npm run build`. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: the tray app's Sessions tab shows the dashboard itself (#161)`.

### Task 9: The Windows build, the operator checklist, and the whole-branch check

**Files:**
- Create: `tray/OPERATOR-VERIFY.md`. Modify: `tray/README.md`, and the root `README.md` (one line pointing at `tray/`).

- [ ] **Step 1: Build.** From WSL, run `cd tray && npm run package:win`. Expected: `release/win-unpacked/CRR.exe` exists. Builds use wine, as the Qwen launcher's do.
- [ ] **Step 2: Write `OPERATOR-VERIFY.md`.** Write it in the Qwen launcher's style: numbered checks, each with an expected result. It must cover these checks:
  1. The tray icon appears, and its states follow `crr service stop`/`start` and a crashed session.
  2. After `wsl --shutdown`, the icon shows WSL down, and `wsl -l --running` stays empty for 2 minutes with the app open.
  3. With `crr holder pause 10`, the app makes no `wsl.exe -d` call. Check with Process Monitor or by watching `wsl -l --running` after a shutdown.
  4. Start CRR from the tray boots WSL and starts the units, and Stop CRR stops them, with tmux sessions surviving.
  5. The Control tab rows, the distro picker, and a QR code that scans from a phone.
  6. The Sessions tab shows the dashboard. A login survives an app restart. An external link opens the default browser.
  7. Closing the window hides it, and Quit tray app ends the app.
  8. Start at login launches hidden.
- [ ] **Step 3:** Run `npm run typecheck && npm test && npm run build` in `tray/`, and the repo's `pytest` (with the #176 deselect) and `lint-imports`. Expected: all green.
- [ ] **Step 4:** Open the PR with `Refs #161`, wait for green CI including the new `tray` job, and merge. The owner runs `OPERATOR-VERIFY.md` on Windows; any failure there becomes an issue or a fix PR.
