"""Version-ledger guard (#38 — audit run-3 P9/P6).

A version constant's comment block IS this project's drift record: the
whole point of bumping is that a reader can later ask "what changed at
v7?" and get an answer. Run 3 found three shape changes with no recorded
reason and one prior reason deleted outright, which makes the ledger worse
than absent — it looks complete while having holes.

These tests make the ledger mechanically checkable: every version from the
floor to the shipped constant must be accounted for, either by an entry
saying what it added or by an explicit `(skipped)` note saying it never
shipped. Silence is no longer a valid state.
"""

import re
from pathlib import Path

from crr.core import config as cfg
from crr.core import contracts

_SRC = Path(__file__).resolve().parent.parent / "crr" / "core"


def _ledger_versions(path: Path, constant: str) -> set[int]:
    """Versions with an entry in the comment block above ``constant``."""
    text = path.read_text(encoding="utf-8")
    head = text[: text.index(constant)]
    return {int(n) for n in re.findall(r"^#\s*v(\d+)\b", head, re.M)}


def _assert_contiguous(path: Path, constant: str, current: int, floor: int = 2) -> None:
    have = _ledger_versions(path, constant)
    missing = sorted(v for v in range(floor, current + 1) if v not in have)
    assert not missing, (
        f"{path.name} {constant} = {current}: no ledger entry for v{missing}. "
        "Every bump records what changed — or says '(skipped, never shipped)' "
        "if the number was burned. A hole here is undocumented drift."
    )


def test_sessions_contract_ledger_has_no_holes():
    # v5 (last_reply) and v6 (title+slug) shipped undocumented, and v3's
    # entry ("adds the per-session nullable tmux_session field") was deleted
    # by a later edit rather than superseded.
    _assert_contiguous(_SRC / "contracts.py", "SESSIONS_CONTRACT_VERSION",
                       contracts.SESSIONS_CONTRACT_VERSION, floor=3)


def test_diagnostics_contract_ledger_has_no_holes():
    _assert_contiguous(_SRC / "contracts.py", "DIAGNOSTICS_CONTRACT_VERSION",
                       contracts.DIAGNOSTICS_CONTRACT_VERSION)


def test_config_defaults_ledger_has_no_holes():
    # The real defect here was not a missing entry but a SKIPPED NUMBER:
    # 553134e bumped 8 -> 10 in one step while labelling its entry "v9", so
    # every comment after it sat one behind the constant it described.
    _assert_contiguous(_SRC / "config.py", "CONFIG_DEFAULTS_VERSION",
                       cfg.CONFIG_DEFAULTS_VERSION)


def test_status_docstring_version_matches_the_shipped_contract():
    """The drift that regressed: run-2 F1 fixed "contract v2" -> v3, and the
    same docstring was later found saying v4 against a shipped v8."""
    text = (_SRC / "status.py").read_text(encoding="utf-8")
    claimed = re.search(r"payload \(contract v(\d+)\)", text)
    assert claimed, "status.py's module docstring no longer names its contract version"
    assert int(claimed.group(1)) == contracts.SESSIONS_CONTRACT_VERSION
