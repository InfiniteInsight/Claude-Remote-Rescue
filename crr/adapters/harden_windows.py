"""Windows Update hardening adapter — reads the registry, nothing else.

crr's hardening assessment (``crr.core.harden``) needs to know whether the
"do not auto-reboot while logged on" policy and active hours are set. This
module is the READ half: a pure parser plus a thin unelevated PowerShell
runner, matching the shape of ``diagnostics_windows.py``. The WRITE half
(``--apply``) is a separate task and is never invoked here.

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
