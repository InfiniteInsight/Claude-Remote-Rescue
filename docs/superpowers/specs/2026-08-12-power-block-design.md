# Power blocking — keep the machine up while a Claude session is live

**Status:** design · 2026-08-12
**Issues:** new work
**Scope:** phase 1 of 3 — headless holds (all platforms) + the Windows tray.
Linux tray and the macOS GUI agent are named here and specified separately.

---

## Why

crr's entire provenance is *recovery*: a Windows-Update reboot and a WSL VM
OOM death, both survived by reviving sessions afterwards (DESIGN.md,
"Provenance"). This is the first feature that tries to stop the loss
instead of repairing it.

The builder's words:

> I just want shutdown and sleep to be blocked when claude is open and
> running.

and, on the exception that matters:

> None of them should block a sleep that happens when someone closes a
> computer lid.

A closed lid is an explicit "I am done". Everything else — idle timeout,
Start▸Restart, Windows Update at 03:00 — is the machine deciding on your
behalf while work is in flight.

## What was measured, and what was wrong

Every claim below was measured on the builder's host (WSL 2.7.10.0 on
Windows 11, laptop, `HedyLamarr`) on 2026-08-12. Two earlier assertions in
this design's own discussion were **wrong and are corrected here**,
recorded because the corrections shaped the design:

| claim | verdict |
|---|---|
| "A headless tool cannot block a Windows restart" | **Wrong.** An unelevated PowerShell launched from WSL registered a block: `create=True lastError=0 elevated=False` |
| "WSL probably skips the systemd shutdown sequence, so a shutdown hook is dead weight" | **Wrong.** The previous boot reached `shutdown.target`, `final.target` and `poweroff.target` cleanly. The GitHub issues saying otherwise are old and closed |
| `systemd-inhibit` block mode, unprivileged, in WSL | `idle`/block ✅, `sleep`/delay ✅, `shutdown`/delay ✅; `sleep`/block ❌, `shutdown`/block ❌ — denied because this shell has no logind session, not because Linux forbids it |
| AC detection | WSL2 passes the host battery through sysfs: `/sys/class/power_supply/AC1/online = 1`, agreeing with Windows `BatteryStatus=2`. **One Linux adapter covers native Linux and WSL** |
| Windows Update exposure | Active hours are **7–19**, smart-hours off. Sessions run past midnight, so every night is outside the protected window |
| `NoAutoRebootWithLoggedOnUsers` | **Not set.** The `...WindowsUpdate\AU` policy key does not exist |

The lesson from #65 and #72 applies: platform behaviour gets measured, not
reasoned about. Where this document could not measure, it says so.

## Capability, honestly

Runtime = crr holds it automatically, no privileges, released when the last
session ends. Hardening = a one-time privileged host setting `crr harden`
walks the user through.

| | sleep | restart |
|---|---|---|
| **Linux** | runtime, complete — `systemd-inhibit --what=sleep`; the lid still works, see below | runtime, complete — `--what=shutdown --mode=block`; `systemctl reboot` refuses and names crr |
| **Windows / WSL** | runtime blocks idle sleep (`ES_SYSTEM_REQUIRED`) | runtime blocks interactive restart (`ShutdownBlockReasonCreate`); **Windows Update needs hardening**, then measurement |
| **macOS** | runtime blocks idle (`caffeinate -i`) | **unavailable.** Deferred — see "macOS" below |

**Lid close is never blocked, on any platform.** On Windows and macOS the
runtime mechanisms already ignore the lid. `pmset -a disablesleep 1` is
excluded from macOS hardening because it would block the lid.

### Linux: it is `sleep`, not `idle` — and that is the opposite of the obvious choice

An earlier draft of this design specified `--what=idle` and proposed a
regression test *enforcing* that `sleep` never appear, on the reasoning
that a `sleep` inhibitor would block lid close. Both halves were wrong, per
`logind.conf(5)`:

| setting | default | consequence |
|---|---|---|
| `LidSwitchIgnoreInhibited=` | **`yes`** | logind suspends on lid close **even while a `sleep` block inhibitor is held** |
| `IdleAction=` | **`ignore`** | logind performs no idle action at all unless configured |
| `HandleSuspendKeyIgnoreInhibited=` | `no` | the suspend key *does* respect inhibitors |

So `--what=idle` inhibits `IdleAction`, which is **disabled by default** —
it would hold successfully, report success, and protect nothing. That is
the silent-false-claim failure this project exists to avoid, and the
proposed test would have locked it in.

`--what=sleep` is correct. GNOME and KDE suspend on idle by asking logind
to `Suspend()` as an unprivileged user, which is precisely what a `sleep`
lock inhibits — and the lid remains exempt by default.

**Two conditions the implementation must check rather than assume**, because
both are defaults a user can change:

- If `LidSwitchIgnoreInhibited=no` on this host, a `sleep` lock *does*
  block the lid, violating the builder's explicit requirement. crr reads
  the effective value and, if it is `no`, refuses the sleep hold and says
  why rather than silently blocking the lid.
- If `IdleAction=` is set to something other than `ignore`, `idle` becomes
  meaningful and can be added alongside.

## Architecture

Existing layering, no new patterns:

```
crr/core/power.py          pure: (live_sessions, on_ac, config) -> Decision
crr/core/ports.py          PowerHolder, PowerSource
crr/adapters/power_source.py
crr/adapters/power_hold_{linux,windows,macos}.py
crr/cli.py                 selects the adapter; owns crr-awake
```

### Core (pure, no I/O)

```python
@dataclass(frozen=True)
class Decision:
    want: frozenset[str]      # subset of {"sleep", "shutdown"}
                              # "sleep" means AUTOMATIC sleep only —
                              # lid close is never in scope anywhere
    reason: str               # shown in the OS's own blocking UI
    withheld: str | None      # why nothing is held, for doctor
```

`decide(live_sessions, on_ac, config) -> Decision` is a pure function and
carries the whole policy: config off → nothing; no live session → nothing;
`requires_ac` and not on AC → nothing, with `withheld` explaining which.

`unmet(capabilities, want) -> tuple[str, ...]` names what this platform
cannot deliver so `crr doctor` can state it rather than silently omit it.

### Ports

```python
class PowerSource(Protocol):
    def on_ac(self) -> bool | None: ...

class PowerHolder(Protocol):
    def capabilities(self) -> frozenset[str]: ...
    def hold(self, want: frozenset[str], reason: str) -> None: ...
    def release(self) -> None: ...
    def held(self) -> frozenset[str]: ...
```

`on_ac()` is **tri-state on purpose**. A machine with no battery device is a
desktop — that is a known `True`, not an unknown. `None` means the probe
failed, and per the spine principle an unknown must never become a positive
claim in either direction: on `None`, crr holds nothing and says why.

### Every hold is a child process whose lifetime is the hold

The same property `crr/adapters/locking.py` already argues for: "release on
process death … crr's whole purpose is surviving processes that die badly."

- **Linux** — `systemd-inhibit --what=sleep:shutdown --mode=block …`
- **macOS** — `caffeinate -i`
- **Windows** — one `powershell.exe` holding *both* locks: a
  `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)` for sleep and
  a window's `ShutdownBlockReasonCreate` for restart. In phase 1 that window
  is also the tray icon's window, so the tray and the blocker are one
  object, not two.

### Windows: the window must be visible when it blocks

`ShutdownBlockReasonCreate` returning `TRUE` was measured on this host and
proves only that the reason **registers**. It does not prove the shutdown is
**blocked**. Per "Shutdown Changes for Windows Vista":

> the system does not allow console applications or applications without a
> visible window to cancel shutdown. These applications are automatically
> terminated if they do not respond to `WM_QUERYENDSESSION` or
> `WM_ENDSESSION` within 5 seconds or if they return **FALSE** in response
> to `WM_QUERYENDSESSION`.

A tray app's owner form is normally hidden, so the naive implementation
registers a reason that is then ignored — success reported, nothing
protected, the same failure mode as the Linux `idle` mistake above.

The design therefore makes the window **visible at the moment it matters**:
on `WM_QUERYENDSESSION` the tray app shows its confirmation dialog (below),
which *is* a visible window, and only then returns `FALSE`. The registered
block reason remains, so crr is also named on the Blocked Shutdown Resolver
screen if the 5-second window elapses.

**Still unverified:** whether a *hidden* window's registered reason appears
on the BSDR screen at all. Raymond Chen's account of untitled helper windows
showing up there suggests it does, but that is inference, not measurement,
and the design does not depend on it. Verifying means letting Windows
attempt a real restart; until someone does, `crr doctor` reports the Windows
restart block as **registered, efficacy unverified**.

### Selection: WSL takes the Windows holder, not the Linux one

`platform.system()` on WSL returns `Linux`, so the obvious
`boot_identity.detect()`-shaped selection would pick `systemd-inhibit` —
which runs inside the VM and **cannot affect the Windows host's power state
at all**. It would hold successfully, report success, and protect nothing:
a silent false claim, the exact failure this project's spine principle
forbids.

Selection therefore branches on `host.is_wsl()` (already used by
`_cmd_systemd` for the same class of reason) *before* `platform.system()`:

| host | holder |
|---|---|
| WSL | Windows, via interop |
| native Linux | `systemd-inhibit` |
| macOS | `caffeinate` |
| Windows native | Windows — unreachable today, crr does not run there (#75) |

A test asserts a WSL host resolves to the Windows holder. Without it this
regresses to something that looks like it works.

### The orphan hazard

**This is the most dangerous thing in the design.** On Linux and macOS the
child sits in the unit's cgroup or job and is reaped when crr stops. A
Windows interop child does **not** die with its WSL parent. An orphaned
holder would block restarts forever, with no crr running to explain it —
crr would have manufactured exactly the class of unexplained machine
behaviour it exists to eliminate.

Two defences, because the first is the one that cannot be proven from
Linux:

1. **stdin-EOF exit.** The holder reads stdin until EOF and then exits. When
   the WSL parent dies the pipe closes and both locks release. A mechanism,
   not a timer.
2. **`power_block_max_hours` (default 12).** A hard self-release cap.

A test asserts the holder exits when its stdin is closed. On a host where
that cannot be verified, `crr doctor` reports the cap as the only live
defence rather than implying both work.

### Ownership

A dedicated always-on unit, `crr-awake`, chosen over folding into
`crr-web`: the hold must be owned by a process whose death releases it, and
tying that to the dashboard would couple "am I serving a page" to "may this
machine sleep". It polls the journal for live claude sessions and the power
source, then holds or releases.

## The shutdown prompt

> If a user initiates shutdown we should pop a message asking if they want
> to end crr and allow the shutdown.

**Windows (phase 1).** The tray app receives `WM_QUERYENDSESSION` and shows
its own dialog *before* Windows' Blocked Shutdown Resolver screen:

```
crr — 3 Claude sessions are live
[ Record and shut down ]   [ Cancel shutdown ]
```

"Record and shut down" stamps the journal for every live session — sid,
cwd, boot id, and that they ended in a *clean* shutdown rather than a crash
— then destroys the block reason and lets the shutdown proceed. That
stamping matters because **tmux dies with the machine**: parking into tmux
survives a dead shell, not a dead box. The only thing that outlives a
restart is the journal, so the button's job is to make the journal exact,
which makes the post-reboot rescue prompt complete and correctly attributed
instead of inferred.

"Cancel shutdown" keeps the block and returns `FALSE`.

**Linux (phase 1).** The desktop environment provides the equivalent prompt
— GNOME and KDE both surface "crr is preventing shutdown" with cancel and
force-anyway. crr does not draw its own dialog until the Linux tray exists.
This is a stated limitation, not an oversight.

**Headless hosts.** With no GUI there is no prompt, so the block must never
be a trap. The refusal text names the escape hatch (`crr power release`),
and `crr doctor` prints it whenever a hold is active.

## Windows Update

The runtime block almost certainly does not survive a forced Update
restart; those use a force flag that historically bypasses blockers. This
was **not** verified — verifying it means letting Windows force a restart.

So `crr harden` applies the policy tier:

- `NoAutoRebootWithLoggedOnUsers = 1`
- Active hours widened to cover actual working hours (18h maximum span)

and crr then **measures whether it held**. It already reads boot history, so
after an unexpected restart it can report: *"restarted at 03:12 outside your
active hours, with hardening applied."*

This matters because the policy's efficacy on Windows 11 is genuinely
contested — Microsoft has moved it under "Legacy Policies" and there are
credible reports of it being ignored. crr must not claim protection it
cannot demonstrate. Applying the policy is a best effort; the boot-history
report is the evidence.

`crr harden` prints the exact commands by default and only runs them with
`--apply` after confirmation. crr never writes to HKLM silently.

## Configuration

Off by default. All keys configurable per the existing `config.toml` /
`DEFAULTS` mechanism, which means the config-defaults version bumps.

```toml
power_block             = "off"   # "off" | "sleep" | "sleep+shutdown"
power_block_requires_ac = true
power_block_max_hours   = 12
power_poll_seconds      = 30
```

`power_block_requires_ac` defaults true: a forgotten session must not flatten
an unplugged laptop. The AC probe is proven on both Linux and WSL.

## Visibility

A hold that cannot be seen is a trust hazard — a machine that will not sleep
with no visible cause is the same genre of mystery as the `wt.exe` failure
that started all this.

- **`crr doctor`** — what is held, why, which platform capabilities are
  unavailable, whether hardening is applied, and the release command.
- **Dashboard** — a badge while a hold is active, naming the session count.
- **Tray** — icon state plus the settings surface (phase 1, Windows only).

## macOS

Deferred, with the research attached so the next person does not repeat it.

A menu-bar app *can* cancel a user-initiated logout or restart via
`applicationShouldTerminate:` returning `NSTerminateCancel` — but only if
sudden termination is explicitly disabled in `Info.plist`, which modern
Xcode templates enable by default.

That forces an `.app` bundle (both for that key and for `LSUIElement`),
which means **PyObjC and Swift cost the same bundling work**. PyObjC's only
remaining advantage is language, against a large macOS-only dependency and
demonstrated fragility of Python menu-bar apps on new macOS releases. If
this is ever built, build it in Swift.

It is deferred because the ceiling is low: it blocks a user-initiated
restart — parity with Windows' BSDR — and cannot stop a forced one. That is
substantial Swift, bundling and notarisation work for a speed bump on a
platform with no current users (#43). `crr doctor` states the gap.

## Testing

- **Core** — `decide()` and `unmet()` are pure and exhaustively tested,
  including every `withheld` reason and the tri-state `on_ac` (`None` holds
  nothing).
- **Adapters** — fakes for the holder; the real ones are gated per platform.
  The AC probe is tested against synthetic sysfs trees, including the
  no-battery desktop case and an unreadable-probe case.
- **Orphan defence** — a test spawns the holder, closes its stdin, and
  asserts it exits. This is the load-bearing safety test.
- **Lid** — the Linux inhibitor argument must contain `sleep`, because
  `idle` inhibits `IdleAction`, which is `ignore` by default and therefore
  protects nothing. A second test asserts that when `LidSwitchIgnoreInhibited`
  reads `no` on the host, crr **withholds** the sleep hold rather than
  blocking the lid. Both carry the reasoning in the failure message: an
  earlier draft of this spec got this backwards, and the wrong version looks
  more obviously correct than the right one.
- **Windows visibility** — a test asserts the confirmation dialog is shown
  before `FALSE` is returned to `WM_QUERYENDSESSION`, since an invisible
  window cannot cancel shutdown at all.
- **Windows** — the tray/blocker is exercised on `windows-latest`, which
  rejoined the matrix in #72. What cannot run there is skipped with a stated
  reason, per that issue's convention.

## Out of scope

- Linux tray (StatusNotifierItem; GNOME needs a third-party extension to
  show a tray at all) — own spec.
- macOS GUI agent — own spec, if ever.
- Blocking lid-close sleep — deliberately never.
- Preventing WSL VM OOM death, the other provenance incident — unrelated
  mechanism.
