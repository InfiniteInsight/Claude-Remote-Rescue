"""Windows power hold from WSL, via one PowerShell child.

Both locks live in ONE process so there is one lifetime to reason about:
``SetThreadExecutionState`` for idle sleep, and a window's
``ShutdownBlockReasonCreate`` for restart. Both were measured callable
UNELEVATED from WSL on 2026-08-12.

Seven things this file must never lose:

1. **The stdin-EOF exit.** A Windows interop child does NOT die with its
   WSL parent. Without this wait, a killed crr leaves a PowerShell holding
   a shutdown block forever — a machine that refuses to restart with
   nothing left running to explain why. That is the exact class of
   unexplained behaviour crr exists to eliminate, so the mechanism (not a
   timer) is the primary defence.
2. **``ES_SYSTEM_REQUIRED`` only, never ``ES_DISPLAY_REQUIRED``.** The lid
   must keep working and the screen must be allowed to turn off.
3. **The deadline is ONE bounded async wait on the RAW stdin stream,
   never a loop around a blocking read, and never ``[Console]::In``.**
   ``[Console]::In.ReadLine()`` blocks synchronously, so a
   ``while (deadline) { ReadLine() }`` loop only re-checks the deadline
   *between* completed reads. Since ``hold()`` writes the script once and
   never sends a second line, the process would enter ``ReadLine`` a
   single time and block there for the rest of its life — the deadline
   would never re-fire and ``max_hours`` would be dead code wearing a
   passing string-match test. Confirmed live on 2026-08-12: a holder
   spawned with ``max_hours=0.0006`` (~2.16s), stdin left open with no
   further writes, was still alive at t=25s.

   The first fix attempt swapped in ``[Console]::In.ReadLineAsync()``
   awaited via ``Task.Wait(ms)``. That is ALSO wrong, and was caught the
   same way: measured live on 2026-08-13, the assignment
   ``$readTask = [Console]::In.ReadLineAsync()`` itself did not return
   control for 30+ seconds with stdin held open and no data sent — so
   ``.Wait(ms)`` never even started timing. ``[Console]::In`` wraps a
   ``SyncTextReader``, and ``ReadLineAsync()`` on that reader runs
   synchronously on the calling thread despite returning a ``Task``. The
   working fix reads the RAW stream from
   ``[Console]::OpenStandardInput()`` (unwrapped, genuinely async) via
   ``Stream.ReadAsync`` and bounds it with
   ``[System.Threading.Tasks.Task]::WaitAny(@($readTask), ms)``, which
   returns ``-1`` on timeout (the read stays pending, harmless — the
   process is exiting either way) or the completed index on EOF.
4. **The P/Invoke signature block is ONE PowerShell source line, never a
   here-string.** ``$sig = @" ... "@`` is what the original design and
   the first fix attempt both used. Measured live on 2026-08-13: fed
   through ``powershell.exe -NoProfile -Command -`` over a piped (not
   console) stdin, a ``@" ... "@`` block never executes ANYTHING in the
   script that contains it — not the here-string assignment, not any
   statement before or after it — even after real EOF, even holding
   stdin open for several seconds first. The failure is silent: exit code
   0, zero output, every single time, regardless of the here-string's
   content (confirmed with a trivial one-line body, not just the real
   DllImport signatures). Multi-line constructs that DON'T require the
   parser to buffer across lines (a plain multi-statement script, a
   single-line ``@($x)`` array subexpression) execute immediately and
   correctly over the same piped stdin; only the here-string's inherent
   "keep reading until a line starts with the closing token" parsing was
   silently broken here. The fix builds the C# signature block as ONE
   PowerShell line: a normal double-quoted string with embedded double
   quotes backtick-escaped (`` `" ``) and line breaks inserted via
   PowerShell's own `` `n `` escape, so nothing about the *PowerShell
   source* spans multiple lines even though the *string value* does.
5. **The script ends with an explicit exit, never falls off the end.**
   ``powershell.exe -Command -`` behaves like an interactive session: once
   it finishes running whatever was piped to it, it goes back to reading
   stdin for the *next command*, it does not quit. Confirmed live on
   2026-08-13 with full tracing: every statement in the script — sig
   build, Add-Type, both P/Invoke calls, the WaitAny deadline firing
   (idx=-1) or completing (EOF), both release calls — ran to completion
   in ~2.4s, and the *process* was still alive and reported so 30 seconds
   later, because after the last statement it just waited for another
   command on the same still-open stdin. ``[Environment]::Exit(0)`` as
   the final statement is what actually ends the process. Releasing the
   locks is necessary but not sufficient for "the process is gone" — both
   are required, and they are different lines of PowerShell.
6. **The whole script is ONE PowerShell statement, never multiple
   top-level lines.** This is the one that actually explains why a
   sleep-only hold worked throughout this file's history while a
   sleep+shutdown hold quietly self-released hours early. Our own
   ``$stdin.ReadAsync($buf, 0, 1)`` and ``-Command -``'s own read of "the
   rest of the piped script" pull from the SAME kernel pipe. Whenever
   PowerShell statements remained unparsed after the read (true for
   sleep+shutdown, which has release calls after the wait; not true for a
   bare sleep-only script with nothing left to run), the two reads raced
   for the same bytes. Measured live on 2026-08-13, 100% reproducible for
   a fixed script: the internal read sometimes won that race and stole
   ONE byte meant for our own trailing PowerShell text, corrupting it by
   exactly one dropped character (``SetThreadExecutionState`` read back
   as ``SetThreadExectionState``; ``uint32`` read back as ``unt32``) and
   completing the "wait" almost instantly instead of honouring the
   deadline. The failure mode is NOT an orphaned process — the stolen
   byte still made ``WaitAny`` return, so both release calls still ran
   and the process still exited (just ~1.7 hours early for a 2-hour
   ``max_hours=0.0006`` test, i.e. at ~0.5s instead of ~2.2s) — it is
   ``held()`` reporting a hold that has silently stopped holding
   anything. Wrapping the entire body as ``& { stmt1; stmt2; ...; }``
   forces the parser to consume the complete statement before executing
   any of it, so by the time the internal read fires there is nothing of
   our own script left in the pipe to steal. NOTE: the original design's
   blocking ``[Console]::In.ReadLine()`` had this exact same contention
   whenever ``shutdown`` was requested, since its release calls also
   follow the read — this was never sleep-only-safe either, it was just
   never measured with tracing precise enough to catch a race that a
   sleep-only script cannot exhibit.
7. **``reason`` is sanitized, never trusted, before it reaches the
   script.** Point 6 made the whole script ONE PowerShell statement, which
   means a newline (or any other control character) inside ``reason``
   is no longer cosmetic — it is a second top-level line the parser sees
   as separate from the statement above. Measured live 2026-08-13: an
   unsanitized ``reason`` containing ``\n`` produced a script that
   silently executed NOTHING at all when piped to the real host — alive
   at 12s against a 2.16s deadline, exit 0, zero stderr, only EOF ended
   it, i.e. ``held()`` reporting BOTH locks acquired with ZERO
   protection actually in place. This is the worst outcome the whole
   design can produce — reporting protection that does not exist — so it
   is fixed even though nothing in ``crr`` calls ``hold()`` with an
   attacker- or user-controlled ``reason`` today (the sole producer,
   ``crr/core/power.py``, builds a fixed ``"crr: N Claude session(s)
   live"`` string). ``reason`` is cosmetic display text for the OS's
   blocking UI, so a bad ``reason`` must degrade the MESSAGE, never the
   HOLD: ``_sanitize_reason`` strips every control character (0x00-0x1F,
   plus DEL) to a space, collapses the resulting whitespace runs, and
   only THEN doubles single quotes for the PowerShell string literal —
   never raises.

KNOWN LIMIT, recorded rather than assumed: registration returning TRUE
proves the reason REGISTERS, not that shutdown is BLOCKED. Microsoft is
explicit that an application without a visible window cannot cancel
shutdown. Making the window visible at the moment it matters is the tray
plan's job; this holder registers the reason and reports its efficacy as
unverified.

SECOND KNOWN LIMIT, same shape: ``held()`` is a LIVENESS poll, not proof of
acquisition. It reports the requested set whenever the child process is
still running, and nothing reads back from the child to confirm the two
API calls actually succeeded. If ``Add-Type`` fails under
``$ErrorActionPreference='Stop'`` -- a missing assembly, a constrained
language mode, an AppLocker policy -- the ``& { ... }`` statement aborts
before either lock is taken, yet the PowerShell host process stays alive
reading stdin, so ``held()`` reports BOTH locks while ZERO protection is
in place. Closing this would need the child to write an acquisition
receipt back over stdout and ``held()`` to require it. Until then the
limit is recorded rather than assumed, which is what makes it shippable:
a consumer must treat ``held()`` as "the holder is running", not as "the
machine is protected".
"""

from __future__ import annotations

import re
import subprocess

from crr.core.config import DEFAULTS

_ES_CONTINUOUS = "0x80000000"
_ES_SYSTEM_REQUIRED = "0x00000001"
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED, precomputed and embedded as a single
# literal so the script text carries the combined flag value directly
# rather than an -bor expression PowerShell evaluates at runtime. Same
# numeric value either way; this form is what the test (and a reader
# grepping the emitted script) can see without evaluating PowerShell.
_ES_SLEEP_FLAGS = "0x80000001"

# Task.Wait(int millisecondsTimeout) takes a signed Int32. A caller passing
# an absurd max_hours must not overflow into a negative value -- .NET
# either rejects a negative timeout outright or, for -1 specifically,
# treats it as "wait forever" (the exact opposite of a backstop). Clamp
# defensively at both ends.
_INT32_MAX_MS = 2147483647

# Teardown budget. Measured normal teardown is ~2.07s (stdin close -> EOF ->
# both release calls -> [Environment]::Exit(0)), so blowing a 10s wait means
# something is genuinely wrong and escalating beats swallowing it: the
# process may still hold ShutdownBlockReasonCreate.
_RELEASE_WAIT_SECONDS = 10
_FORCE_WAIT_SECONDS = 5

# Control characters (0x00-0x1F, plus DEL) that must never reach the
# emitted script unescaped: a newline or carriage return in `reason`
# breaks the one-PowerShell-statement invariant (module docstring, point
# 6) by adding a top-level line the parser will not see as part of the
# same statement. Measured live 2026-08-13: with a raw `\n` in `reason`,
# the emitted 3-line script silently executed NOTHING when piped to the
# real host -- alive well past its deadline, exit 0, zero stderr, only
# EOF ended it. held() would report both locks acquired while nothing
# was actually held. `reason` is cosmetic display text for the OS's
# blocking UI, so a bad reason must degrade the MESSAGE, never the HOLD.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _sanitize_reason(reason: str) -> str:
    """Make ``reason`` safe to embed in the ONE-line script, without ever
    raising: strip control characters (collapsing the whitespace they
    leave behind), THEN double any single quotes for the PowerShell
    string literal. Order matters -- quote-doubling first would not help
    a newline, and control-stripping after quote-doubling could still
    leave a stray control character sitting next to a doubled quote.
    """
    no_control = _CONTROL_CHARS_RE.sub(" ", reason)
    collapsed = _WHITESPACE_RUN_RE.sub(" ", no_control).strip()
    return collapsed.replace("'", "''")


def _timeout_ms(max_hours: float) -> int:
    """``max_hours`` in milliseconds, clamped to fit a signed 32-bit int.

    ``round()``, not ``int()`` truncation: ``0.0006 * 3600 * 1000`` is
    ``2159.9999999999995`` as a float, and truncating silently emits a
    literal (``2159``) that does not match the arithmetic it was derived
    from -- exactly the kind of unrecorded drift this codebase's
    provenance rules exist to catch, even though a 1ms error is harmless
    at the values this is actually called with.
    """
    ms = round(max_hours * 3600 * 1000)
    return max(0, min(ms, _INT32_MAX_MS))


def holder_argv() -> list[str]:
    """PowerShell, with stdin left open as the liveness channel."""
    return ["powershell.exe", "-NoProfile", "-Command", "-"]


def _ps_oneline_string(csharp_lines: list[str]) -> str:
    """A SINGLE PowerShell source line whose STRING VALUE holds ``\\n``-
    joined C# lines.

    Not a here-string (``@" ... "@``): measured live on 2026-08-13, that
    construct silently executes nothing at all when piped to
    ``powershell.exe -Command -`` over a non-console stdin (see the module
    docstring, point 4). Embedded ``"`` is backtick-escaped and line
    breaks are PowerShell's own ``` `n ``` escape, so the *value* is
    multi-line C# while the *source* stays one line.
    """
    escaped = (line.replace('"', '`"') for line in csharp_lines)
    return '"' + "`n".join(escaped) + '"'


def holder_script(want: frozenset[str], reason: str,
                  max_hours: float = DEFAULTS["power_block_max_hours"]) -> str:
    """The PowerShell program that holds the locks until stdin closes.

    The body is emitted as ONE PowerShell statement -- see point 6 in the
    module docstring for why: our own internal stdin read and
    ``-Command -``'s own read of "the rest of the script" pull from the
    SAME kernel pipe, and any of our script left unparsed when our read
    fires is exactly what that read can steal a byte from.
    """
    safe = _sanitize_reason(reason)
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
    stmts = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Windows.Forms",
        f"$sig = {_ps_oneline_string(sig_lines)}",
        "$api = Add-Type -MemberDefinition $sig -Name CrrHold "
        "-Namespace CrrPower -PassThru",
    ]
    if "sleep" in want:
        stmts.append(
            f"$null = $api::SetThreadExecutionState([uint32]'{_ES_SLEEP_FLAGS}')"
        )
    if "shutdown" in want:
        stmts += [
            "$form = New-Object System.Windows.Forms.Form",
            "$handle = $form.Handle",
            f"$null = $api::ShutdownBlockReasonCreate($handle, '{safe}')",
        ]
    timeout_ms = _timeout_ms(max_hours)
    # THE orphan defence: ONE asynchronous read on the RAW stdin stream,
    # bounded by the full deadline. WaitAny returns -1 on timeout (the
    # WSL parent is presumably alive but max_hours elapsed; the pending
    # read is abandoned harmlessly since the process exits right after)
    # or the completed index on EOF (the WSL parent died and closed the
    # pipe) -- either way execution falls through to the release
    # statements below.
    #
    # Deliberately NOT `while (deadline) { ReadLine() }`: ReadLine()
    # blocks synchronously, so a loop around it only re-checks the
    # deadline between completed reads and never re-fires while blocked
    # in the one and only read.
    #
    # Deliberately NOT [Console]::In (ReadLine or ReadLineAsync):
    # [Console]::In wraps a SyncTextReader, and ReadLineAsync() on it
    # runs synchronously on the calling thread despite returning a Task
    # -- measured live, the assignment itself did not return for 30+
    # seconds with stdin open and no data, so a .Wait(ms) after it never
    # got a chance to time out. OpenStandardInput() returns the raw,
    # unwrapped Stream, whose ReadAsync() is genuinely async.
    #
    # Do not re-issue a read in a loop -- multiple pending reads on the
    # same stream is its own bug.
    stmts += [
        "$stdin = [Console]::OpenStandardInput()",
        "$buf = New-Object byte[] 1",
        "$readTask = $stdin.ReadAsync($buf, 0, 1)",
        "$null = [System.Threading.Tasks.Task]::WaitAny("
        f"@($readTask), [int]{timeout_ms})",
    ]
    if "shutdown" in want:
        stmts.append("$null = $api::ShutdownBlockReasonDestroy($handle)")
    if "sleep" in want:
        stmts.append(
            f"$null = $api::SetThreadExecutionState([uint32]'{_ES_CONTINUOUS}')"
        )
    # [Environment]::Exit(0), not a bare `exit`: `powershell.exe
    # -Command -` behaves like an interactive session and does not quit
    # when it runs out of piped script -- it goes back to reading stdin
    # for the NEXT command. Releasing the locks above is necessary but
    # not sufficient for "the orphan is gone"; this is what actually
    # ends the process once the deadline fires or EOF arrives.
    stmts.append("[Environment]::Exit(0)")
    # A leading comment line, OUTSIDE the statement below, documents
    # max_hours for a human reading the emitted script. It cannot go
    # INSIDE the "; "-joined statement: `#` comments to end-of-line, and
    # since the statement is deliberately kept on ONE line (see the
    # docstring above), an inline comment would silently comment out
    # every statement after it -- including the release calls and the
    # exit.
    body = "; ".join(stmts)
    return f"# max_hours={max_hours}\n& {{ {body} }}\n"


class WindowsPowerHolder:
    # max_hours defaults to the versioned config prior, never a repeated
    # literal: `power_block_max_hours` is already a named key, and a second
    # copy here is a second answer to the same question.
    def __init__(self, spawn=None,
                 max_hours: float = DEFAULTS["power_block_max_hours"]) -> None:
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
        # Tracked BEFORE the write, not after. A BrokenPipeError on the
        # write would otherwise leave a spawned child with no handle to it
        # -- and a Windows interop child does not die with its WSL parent,
        # so "untracked" means "unkillable".
        self._proc = proc
        script = holder_script(effective, reason, max_hours=self._max_hours)
        if getattr(proc, "stdin", None) is not None:
            proc.stdin.write(script)
            proc.stdin.flush()
            # Deliberately NOT closed: the open pipe is the liveness
            # signal. Closing it here would make the holder exit at once.
        # Claimed only once the script actually reached the child: a
        # process that never received the script holds nothing.
        self._held = effective

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def held(self) -> frozenset[str]:
        return self._held if self._alive() else frozenset()

    @staticmethod
    def _reaped(proc, timeout: float) -> bool:
        """True only when ``wait`` actually returned -- i.e. confirmed dead."""
        try:
            proc.wait(timeout=timeout)
            return True
        except Exception:
            return False

    @staticmethod
    def _signal(proc, name: str) -> None:
        try:
            getattr(proc, name)()
        except Exception:
            pass

    def release(self) -> None:
        """Close stdin, then escalate until the child is CONFIRMED reaped.

        The handle is dropped only on confirmation. The old version fired
        terminate() and then cleared ``_proc``/``_held`` unconditionally
        with no re-wait: crr was left with no handle to a PowerShell that
        may still hold ``ShutdownBlockReasonCreate``, permanently
        uncleanable, ``held()`` reporting nothing held, and the user facing
        a machine that refuses to restart with nothing left to explain why.

        Deliberately no ``finally:`` around the bookkeeping -- that is
        exactly the shape that reinstates the bug.
        """
        proc = self._proc
        if proc is None:
            self._held = frozenset()
            return
        try:
            if getattr(proc, "stdin", None) is not None:
                proc.stdin.close()             # EOF -> the script unwinds
        except Exception:
            pass
        if not self._reaped(proc, _RELEASE_WAIT_SECONDS):
            self._signal(proc, "terminate")
            if not self._reaped(proc, _FORCE_WAIT_SECONDS):
                self._signal(proc, "kill")
                if not self._reaped(proc, _FORCE_WAIT_SECONDS):
                    # Neither terminate nor kill confirmed it dead. KEEP
                    # the handle and KEEP reporting the set: a live child
                    # may genuinely still be holding, and the next hold()
                    # retries release(). Reporting an empty hold here would
                    # be the same lie, just quieter.
                    return
        self._proc = None
        self._held = frozenset()
