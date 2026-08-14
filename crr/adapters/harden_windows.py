"""Windows Update hardening adapter — reads the registry, plus the WRITE
half's command builder.

crr's hardening assessment (``crr.core.harden``) needs to know whether the
"do not auto-reboot while logged on" policy and active hours are set. This
module is the READ half: a pure parser plus a thin unelevated PowerShell
runner, matching the shape of ``diagnostics_windows.py``.

``apply_commands`` is the WRITE half's command BUILDER only -- it returns
argv, it never runs anything. Only ``crr.cli._run_commands`` may execute
those commands, and only after the user has confirmed at a terminal (spec
2026-08-14, Task 6).

Three states, not two, per field:
- the registry key/value does not exist -> a KNOWN answer (False for the
  booleans; the policy genuinely is not set)
- the command failed or the output could not be parsed -> unknown (None),
  because we have no idea what the machine's state actually is

Collapsing those two cases would make ``crr.core.harden.assess`` either lie
("policy not set" on a machine we simply couldn't read) or go silent on a
readable machine ("unknown" when the honest answer is available). Both are
worse than the tri-state.
"""

from __future__ import annotations

from crr.adapters._proc import run_capture as _run
from crr.core.harden import HardenState

# PowerShell prints exactly these four `key=value` lines, in order.
# policy=absent when the AU key/value does not exist at all -- distinct
# from policy=0, which means the key exists and is explicitly disabled.
_SCRIPT = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "$auPath = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU'; "
    "$au = Get-ItemProperty -Path $auPath -Name NoAutoRebootWithLoggedOnUsers "
    "-ErrorAction SilentlyContinue; "
    "if ($au) { \"policy=$($au.NoAutoRebootWithLoggedOnUsers)\" } "
    "else { 'policy=absent' }; "
    "$uxPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'; "
    "$ux = Get-ItemProperty -Path $uxPath -ErrorAction SilentlyContinue; "
    "\"ActiveHoursStart=$($ux.ActiveHoursStart)\"; "
    "\"ActiveHoursEnd=$($ux.ActiveHoursEnd)\"; "
    "if ($ux -and $ux.PSObject.Properties['SmartActiveHoursState']) "
    "{ \"SmartActiveHoursState=$($ux.SmartActiveHoursState)\" } "
    "else { 'SmartActiveHoursState=absent' }"
)


def read_command() -> list[str]:
    """Unelevated PowerShell to print the four hardening-relevant values.

    Reading HKLM policy/UX keys does not require elevation; asking for it
    anyway would put a UAC prompt in front of what should be a plain status
    read.
    """
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT]


def _parse_int(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def parse_state(text: str) -> HardenState:
    """Parse the four ``key=value`` lines into a :class:`HardenState`.

    Any field that is missing or does not parse becomes ``None`` (unknown),
    never a guessed default. ``policy=absent`` is the one line that maps to
    a KNOWN ``False`` -- the key not existing IS the answer.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    policy_raw = values.get("policy")
    if policy_raw is None:
        policy_set: bool | None = None
    elif policy_raw == "absent":
        policy_set = False
    else:
        policy_int = _parse_int(policy_raw)
        policy_set = None if policy_int is None else policy_int != 0

    active_start = _parse_int(values.get("ActiveHoursStart", ""))
    active_end = _parse_int(values.get("ActiveHoursEnd", ""))

    smart_field = values.get("SmartActiveHoursState")
    if smart_field is None:
        smart_hours: bool | None = None
    elif smart_field == "absent":
        smart_hours = False
    else:
        smart_int = _parse_int(smart_field)
        smart_hours = None if smart_int is None else smart_int != 0

    return HardenState(
        policy_set=policy_set,
        active_start=active_start,
        active_end=active_end,
        smart_hours=smart_hours,
    )


def read_state(timeout: float, run=None) -> HardenState:
    """Run ``read_command`` and parse it; any failure is all-unknown.

    ``run`` is injectable (signature ``(argv, timeout) -> str``) so tests
    never spawn a real ``powershell.exe``; defaults to the shared
    subprocess runner used by the rest of the adapters.
    """
    run = run or _run
    try:
        text = run(read_command(), timeout)
    except Exception:
        return HardenState(None, None, None, None)
    return parse_state(text)


# Same HKLM paths ``read_command``'s script reads, deliberately kept
# identical -- a write to a different key would parse_state() forever after
# as "not set", the exact "succeeds loudly, protects nothing" shape this
# module's read half exists to catch.
_AU_PATH = "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU"
_UX_PATH = "HKLM\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings"


def _elevated_reg_add(path: str, name: str, value: int) -> list[str]:
    """One argv: an unelevated ``powershell.exe`` that re-launches
    ``reg.exe`` elevated via ``Start-Process -Verb RunAs``, so Windows
    shows a UAC prompt for the HKLM write. HKLM writes silently no-op
    without elevation -- without ``RunAs`` here, crr would report the
    write as run and the registry would be untouched.

    Fix round 1: ``Start-Process ... -Wait`` alone does NOT propagate the
    child's exit code -- the outer ``powershell.exe`` exits 0 regardless of
    whether ``reg.exe`` succeeded, and a declined UAC prompt raises a
    non-terminating ``InvalidOperationException`` that ``powershell.exe``
    also swallows into exit 0. Measured read-only on a real host: a failing
    ``reg.exe`` invocation exits 1, but the un-fixed wrapper still exited 0.
    ``_run_commands`` only inspects ``returncode``, so that shape is this
    feature's signature defect (succeeds loudly, protects nothing) landing
    in the one path that changes the machine. ``-PassThru`` captures the
    child process object so its real ``ExitCode`` can be read;
    ``exit $p.ExitCode`` is what actually forwards a failing ``reg.exe``
    exit code outward when ``Start-Process`` itself succeeded.

    Fix round 2: ``$ErrorActionPreference = 'Stop'`` is meant to turn a
    declined-UAC prompt's non-terminating exception into a terminating one
    (which would exit nonzero on its own, before ``exit $p.ExitCode`` is
    ever reached) -- but whether ``Start-Process``'s exception actually
    honours ``Stop`` is untestable without a real UAC prompt, and getting
    it wrong would leave ``$p`` as ``$null``: ``exit $null`` is ``exit 0``,
    the exact bug this function exists to close. So the null case is
    guarded explicitly rather than trusted to ``Stop`` -- correct whether
    or not the promotion happens, with no untested dependency either way.
    """
    reg_args = f'add "{path}" /v {name} /t REG_DWORD /d {value} /f'
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$p = Start-Process reg.exe -ArgumentList '{reg_args}' -Verb RunAs "
        "-Wait -PassThru; "
        "if (-not $p) { exit 1 }; "
        "exit $p.ExitCode"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def apply_commands(want_start: int, want_end: int) -> list[list[str]]:
    """Build (never run) the elevated writes for both hardening levers:
    ``NoAutoRebootWithLoggedOnUsers`` and the active-hours start/end.

    Returns argv lists only. Executing them is ``crr.cli``'s job, and only
    after the user confirms at a terminal -- see the module docstring.
    """
    return [
        _elevated_reg_add(_AU_PATH, "NoAutoRebootWithLoggedOnUsers", 1),
        _elevated_reg_add(_UX_PATH, "ActiveHoursStart", want_start),
        _elevated_reg_add(_UX_PATH, "ActiveHoursEnd", want_end),
    ]
