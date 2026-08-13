"""Windows power hold from WSL, via one PowerShell child.

Both locks live in ONE process so there is one lifetime to reason about:
``SetThreadExecutionState`` for idle sleep, and a window's
``ShutdownBlockReasonCreate`` for restart. Both were measured callable
UNELEVATED from WSL on 2026-08-12.

Two things this file must never lose:

1. **The stdin-EOF exit.** A Windows interop child does NOT die with its
   WSL parent. Without this loop, a killed crr leaves a PowerShell holding
   a shutdown block forever — a machine that refuses to restart with
   nothing left running to explain why. That is the exact class of
   unexplained behaviour crr exists to eliminate, so the mechanism (not a
   timer) is the primary defence.
2. **``ES_SYSTEM_REQUIRED`` only, never ``ES_DISPLAY_REQUIRED``.** The lid
   must keep working and the screen must be allowed to turn off.

KNOWN LIMIT, recorded rather than assumed: registration returning TRUE
proves the reason REGISTERS, not that shutdown is BLOCKED. Microsoft is
explicit that an application without a visible window cannot cancel
shutdown. Making the window visible at the moment it matters is the tray
plan's job; this holder registers the reason and reports its efficacy as
unverified.
"""

from __future__ import annotations

import subprocess

_ES_CONTINUOUS = "0x80000000"
_ES_SYSTEM_REQUIRED = "0x00000001"
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED, precomputed and embedded as a single
# literal so the script text carries the combined flag value directly
# rather than an -bor expression PowerShell evaluates at runtime. Same
# numeric value either way; this form is what the test (and a reader
# grepping the emitted script) can see without evaluating PowerShell.
_ES_SLEEP_FLAGS = "0x80000001"


def holder_argv() -> list[str]:
    """PowerShell, with stdin left open as the liveness channel."""
    return ["powershell.exe", "-NoProfile", "-Command", "-"]


def holder_script(want: frozenset[str], reason: str,
                  max_hours: int = 12) -> str:
    """The PowerShell program that holds the locks until stdin closes."""
    safe = reason.replace("'", "''")
    # Only declare the P/Invoke signatures for what `want` actually calls.
    # Declaring ShutdownBlockReasonCreate/Destroy on a sleep-only hold
    # would be harmless at runtime but makes the emitted script lie about
    # what the process is going to do -- and a sleep-only script that
    # mentions ShutdownBlockReasonCreate is indistinguishable, on
    # inspection, from one that registers a block it doesn't release.
    sig_lines: list[str] = []
    if "sleep" in want:
        sig_lines += [
            '[DllImport("kernel32.dll", SetLastError=true)]',
            "public static extern uint SetThreadExecutionState(uint esFlags);",
        ]
    if "shutdown" in want:
        sig_lines += [
            '[DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)]',
            "public static extern bool ShutdownBlockReasonCreate(IntPtr hWnd, string pwszReason);",
            '[DllImport("user32.dll", SetLastError=true)]',
            "public static extern bool ShutdownBlockReasonDestroy(IntPtr hWnd);",
        ]
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Windows.Forms",
        "$sig = @\"",
        *sig_lines,
        '"@',
        "$api = Add-Type -MemberDefinition $sig -Name CrrHold "
        "-Namespace CrrPower -PassThru",
    ]
    if "sleep" in want:
        lines.append(
            f"$null = $api::SetThreadExecutionState([uint32]'{_ES_SLEEP_FLAGS}')"
        )
    if "shutdown" in want:
        lines += [
            "$form = New-Object System.Windows.Forms.Form",
            "$handle = $form.Handle",
            f"$null = $api::ShutdownBlockReasonCreate($handle, '{safe}')",
        ]
    lines += [
        # THE orphan defence: when the WSL parent dies the pipe closes,
        # ReadLine returns $null, and every lock is released below.
        f"$deadline = (Get-Date).AddHours({max_hours})",
        "while ((Get-Date) -lt $deadline) {",
        "  $line = [Console]::In.ReadLine()",
        "  if ($line -eq $null) { break }",
        "}",
    ]
    if "shutdown" in want:
        lines.append("$null = $api::ShutdownBlockReasonDestroy($handle)")
    if "sleep" in want:
        lines.append(
            f"$null = $api::SetThreadExecutionState([uint32]'{_ES_CONTINUOUS}')"
        )
    return "\n".join(lines) + "\n"


class WindowsPowerHolder:
    def __init__(self, spawn=None, max_hours: int = 12) -> None:
        self._spawn = spawn or (lambda argv, **kw: subprocess.Popen(argv, **kw))
        self._max_hours = max_hours
        self._proc = None
        self._held: frozenset[str] = frozenset()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"sleep", "shutdown"})

    def hold(self, want: frozenset[str], reason: str) -> None:
        effective = frozenset(want) & self.capabilities()
        if effective == self._held and self._alive():
            return
        self.release()
        if not effective:
            return
        proc = self._spawn(
            holder_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        script = holder_script(effective, reason, max_hours=self._max_hours)
        if getattr(proc, "stdin", None) is not None:
            proc.stdin.write(script)
            proc.stdin.flush()
            # Deliberately NOT closed: the open pipe is the liveness
            # signal. Closing it here would make the holder exit at once.
        self._proc = proc
        self._held = effective

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def held(self) -> frozenset[str]:
        return self._held if self._alive() else frozenset()

    def release(self) -> None:
        if self._proc is not None:
            try:
                if getattr(self._proc, "stdin", None) is not None:
                    self._proc.stdin.close()   # EOF -> the script unwinds
                self._proc.wait(timeout=10)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            self._proc = None
        self._held = frozenset()
