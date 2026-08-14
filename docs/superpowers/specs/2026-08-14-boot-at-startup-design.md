# reachable-at-boot — make the control surface survive a reboot, headless

**Status:** design · 2026-08-14
**Issues:** new work. Supersedes the "rolling active-hours" idea (rejected — see Why).
**Scope:** WSL + native Linux **built and verified**; macOS **speced, unverified** (no hardware, plus FileVault — see macOS).

---

## Why

A Windows Update forced-restart at 01:14 killed the builder's remote work. The
first instinct was to *prevent* the reboot. Research killed that: per Microsoft,
"once the effective deadline is reached, the device is forced to restart
**regardless of active hours**." No policy or active-hours trick guarantees
against a deadline-enforced servicing reboot, and the builder's reboot was
exactly that class (`Operating System: Upgrade (Planned)`). Blocking is a false
guarantee.

So the goal flipped from *prevent the reboot* to *make the reboot a non-event*.
crr's whole thesis is "controllable from any device on your tailnet." A reboot
is only catastrophic because the control surface — the dashboard and the
reviver — lives inside a VM (WSL) or a user session that does not wake until
someone physically logs in. Close that gap and a reboot becomes a ~40-second
blip you never have to be home for.

**This was proven on the real machine before speccing.** Two Windows scheduled
tasks were installed by hand; after a cold reboot, with nobody logged in:

- WSL/systemd came up **39 seconds after Windows boot** (previously: not until
  login, 8 hours later),
- the dashboard was reachable from the builder's phone and this very session
  reconnected,
- `AutoAdminLogon` was **not set** and no `DefaultPassword` was stored — *not*
  the autologin security hole the builder explicitly wanted to avoid,
- `LogonUI.exe` was running — the desktop stayed **locked**, PIN required.

The mechanism works and is secure. This spec folds it into crr and extends the
idea to the other platforms.

## Where each platform stands (measured / known)

| platform | survives a reboot today? | mechanism |
|---|---|---|
| **native Linux** | **already** | crr's systemd install runs `loginctl enable-linger`; `crr-web`/`crr-revive` are user units `WantedBy=default.target`; tailscaled is a system service. The dashboard already comes up at boot with no login. |
| **WSL** | not until this ships | a Windows Scheduled Task must boot the WSL VM headless; downstream (linger → crr-web/revive) already works once the VM is up |
| **macOS** | **no** | crr installs **LaunchAgents** (`~/Library/LaunchAgents`), which run only *after* GUI login. Pre-login needs a **LaunchDaemon** (root). And FileVault blocks everything at boot until unlocked. |

## The feature

`crr reachable-at-boot` (name provisional), two halves, the same shape as
`crr harden`: it **reports** by default, **installs** only with a flag and
confirmation, and — the honest half — **measures** whether a real reboot
actually came up headless.

### Install (per platform)

**WSL.** crr detects the distro (`Ubuntu-24.04`), the Windows user (`Infin`),
and the Linux user (`evan`), and generates + registers two Scheduled Tasks. The
exact validated shapes:

- `crr-wsl-boot` — trigger `AtStartup`, principal S4U / RunLevel Highest, action:
  `wsl.exe -d <distro> -u <user> -e sh -c "exec sleep infinity"`, ExecutionTimeLimit
  0 (unbounded — the keepalive holds the VM open past WSL's ~60s idle-shutdown).
- `crr-tailnet-default` (only when >1 Tailscale account is logged in) — trigger
  `AtStartup`, S4U / Highest, runs a small on-disk script that retries
  `tailscale switch <preferred-tailnet>` until tailscaled is ready. The
  preferred tailnet is a config value (`boot_preferred_tailnet`), defaulting to
  the account currently active at install time — crr never silently picks one.
  Keeps a secondary tailnet available for manual `tailscale switch`, which
  persists until the next reboot.

Registering an S4U + AtStartup task needs elevation. crr **generates** the
`Register-ScheduledTask` PowerShell and runs it via `Start-Process -Verb RunAs`
(UAC), only after a tty confirmation — never silently, exactly like
`crr harden --apply`. S4U is used deliberately: it needs no stored password
(the builder logs in with a PIN and has no reusable password), and it kept the
desktop locked in testing.

**Native Linux.** Mostly verification. `--install` ensures linger is enabled
(idempotent; crr already does this) and checks that a boot-time tailscale
service exists. If linger is off, enable it. If tailscale is a *user* service or
absent, say so — the dashboard cannot be reached at boot without a boot-time
tailnet.

**macOS.** Install `crr-web` (at minimum) as a **LaunchDaemon** in
`/Library/LaunchDaemons` (root, `RunAtLoad`, `KeepAlive`) so it starts at boot
before login, plus a boot-time tailscaled. **Loud FileVault refusal:** if
FileVault is enabled, `--install` refuses and explains that Apple runs nothing
at boot until the disk is unlocked at the pre-boot screen — headless survival is
impossible then, and crr will not pretend otherwise. Marked unverified: no Mac
hardware (#43).

### Verify — the honest half

`crr doctor` (and `crr reachable-at-boot` with no flag) runs the exact forensic
that was run by hand, and reports a verdict, never a hopeful "task installed":

- **WSL:** compare Windows boot time (`Win32_OperatingSystem.LastBootUpTime`),
  WSL boot time (journald boot-0 first entry), and the earliest interactive
  login (`quser` / login records). Verdict:
  - WSL came up within a named window of Windows boot (`boot_headless_window_seconds`,
    a config prior — not a magic literal) **and** before any login →
    **headless: confirmed** (a reboot is survivable),
  - WSL came up only at/after login → **login-triggered: NOT surviving** (the
    task did not fire — check it),
  - any timestamp unreadable → **unknown** (never a positive claim).
  It also reports whether the desktop was locked (`LogonUI` present) and that
  autologin is off, so "reachable" is never confused with "an unlocked desktop".
- **Linux:** compare system boot time, `crr-web.service` ActiveEnterTimestamp,
  and earliest login. Same three-way verdict.
- **macOS:** stated as unverified.

This measurement is the point. A task that installs but silently never fires at
boot is the exact "succeeds and protects nothing" failure this project keeps
finding; the forensic is what turns "I installed it" into "I confirmed a reboot
came up headless," proven against real boot timestamps.

## Architecture (existing layering)

```
crr/core/boot_survival.py     pure: what each platform needs; interpret boot
                              timestamps -> a headless/login-only/unknown verdict
crr/adapters/boot_windows.py  generate + register the two Scheduled Tasks; read
                              Windows/WSL boot + login timestamps (interop)
crr/adapters/boot_linux.py    linger state; crr-web ActiveEnterTimestamp; boot +
                              login times
crr/cli.py                    crr reachable-at-boot [--install|--uninstall], doctor
```

Core stays pure (timestamp arithmetic, verdict logic — exhaustively testable
without a machine). Adapters do the I/O. Selection branches on `host.is_wsl()`
before `platform.system()`, as the power feature does, because WSL reports
`Linux` but needs the Windows-side tasks.

## Security stance (must be stated in output)

- **No autologin.** crr never sets `AutoAdminLogon` or stores a password. The
  desktop stays locked; the PIN still gates interactive use. Verified: the
  proven mechanism leaves `LogonUI` up and no `DefaultPassword`.
- **A locked user session exists** on WSL (the VM needs a user context) — this
  is reported honestly, and is strictly better than autologin's unlocked
  desktop.
- **FileVault** on macOS is a hard wall, refused loudly rather than worked
  around.

## Testing

- **Core** — `boot_survival` verdict logic is pure and exhaustively tested:
  headless (boot≈WSL-boot, before login), login-only, and every
  unreadable-timestamp → unknown. Never a positive claim from a missing input.
- **WSL adapter** — the generated `Register-ScheduledTask` text is asserted
  against the validated shape (S4U, AtStartup, Highest, unbounded time limit,
  exact wsl argument). **No test registers a real task or reboots** — generation
  is asserted as text; registration is exercised with an injected runner, the
  same rule the harden plan held.
- **On-host verify** — `crr reachable-at-boot` read-only against this machine must
  report **headless: confirmed** (it just was), and must never render an
  unreadable timestamp as a pass.
- **Linux adapter** — linger detection and the ActiveEnterTimestamp read tested
  against synthetic inputs; the real read gated on Linux.
- **macOS** — plist generation tested with `plistlib`; everything else skipped
  with the FileVault/hardware reason stated, per #43.

## Out of scope

- SSH-into-WSL as a second remote surface (a later layer; the dashboard already
  gives full control once WSL is up).
- Preventing reboots (rejected — no guarantee exists; `crr harden` already
  reports/measures the Update posture for those who want the policy tier).
- Verifying macOS on real hardware (#43).
