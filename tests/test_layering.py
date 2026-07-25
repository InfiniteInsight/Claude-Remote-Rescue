"""Guardrail test: the one-way layering contract is actually enforced.

This does not re-prove import-linter's own correctness; it proves *our*
contract is wired to real modules and evaluated — the failure mode the
audit flagged (a boundary that exists only on paper) is a config that
parses but binds to nothing. If `.importlinter` ever degrades into a
no-op (empty layers, wrong root package, unanalyzable tree), this test
goes red instead of silently passing.

The complementary proof — that a genuine upward import is reported BROKEN
— is done by planting a `core -> adapters` import and running
`lint-imports` by hand (recorded in the Phase 0 verification); it is not
kept in the tree because a permanent violation would keep CI red.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _lint_imports_cmd():
    # Prefer the console script sitting next to the interpreter running the
    # tests (the venv's bin dir), which is where `pip install -e .[dev]`
    # puts it — this is found even when that dir isn't on PATH. Fall back
    # to a PATH lookup so a system install still works.
    beside = Path(sys.executable).parent / "lint-imports"
    if beside.exists():
        return [str(beside)]
    exe = shutil.which("lint-imports")
    if exe:
        return [exe]
    return None


@pytest.mark.skipif(_lint_imports_cmd() is None, reason="import-linter not installed")
def test_layering_contract_holds_and_is_non_trivial():
    result = subprocess.run(
        _lint_imports_cmd(),
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"layering contract is BROKEN:\n{out}"
    assert "1 kept, 0 broken" in out, f"expected exactly one kept contract:\n{out}"
    # Guard against a no-op config that analyzes nothing: the real tree has
    # at least the cli->adapters, cli->core, adapters->core edges. Parse the
    # reported count (word-bounded) so a legit total like "40 dependencies"
    # is not misread as containing "0 dependencies".
    m = re.search(r"Analyzed (\d+) files, (\d+) dependencies", out)
    assert m, f"could not find the analysis summary:\n{out}"
    assert int(m.group(1)) > 0 and int(m.group(2)) > 0, \
        f"contract bound to an empty graph:\n{out}"
