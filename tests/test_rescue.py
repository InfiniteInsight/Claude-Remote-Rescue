"""Tests for crr.core.rescue — rescued-session selection + per-boot marker.

A "rescued" session is a journal entry from a PREVIOUS boot whose
conversation the reviver parked in a currently-live tmux session: crashed
shell, revived claude, awaiting re-homing (Phase-3 restore prompt / `crr
rescued`). These tests pin the pure selection rule and the atomic
claim/stale-cleanup behavior independent of any adapter.
"""

import threading
from pathlib import Path

from crr.core import rescue

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _entry(pid, boot, claude, tmux):
    return {
        "pid": pid,
        "boot_id": boot,
        "cwd": "/home/u/project",
        "claude": (
            {
                "session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
                "sid_source": "injected",
                "started": "2026-07-24T00:00:00Z",
            }
            if claude
            else None
        ),
        "tmux_session": tmux,
    }


def test_rescued_sessions_selects_prior_boot_tmux_parked_only():
    """Phase-3 restore prompt: a candidate is a prior-boot entry whose
    conversation the reviver parked in a LIVE tmux session."""
    e_ok = _entry(pid=2, boot="old", claude=True, tmux="crr-aaaaaaaa")
    e_sameboot = _entry(pid=3, boot="cur", claude=True, tmux="crr-bbbbbbbb")
    e_noclaude = _entry(pid=4, boot="old", claude=False, tmux="crr-cccccccc")
    e_notmux = _entry(pid=5, boot="old", claude=True, tmux=None)
    e_deadtmux = _entry(pid=6, boot="old", claude=True, tmux="crr-dddddddd")
    out = rescue.rescued_sessions(
        [e_deadtmux, e_ok, e_sameboot, e_noclaude, e_notmux],
        current_boot="cur",
        live_tmux={"crr-aaaaaaaa", "crr-bbbbbbbb"},
    )
    assert [e["pid"] for e in out] == [2]


def test_claim_prompt_wins_once_and_stale_cleanup(tmp_path):
    """claim_prompt is the atomic replacement for the old check-then-act
    already_prompted()+mark_prompted() pair (Task-8 fix: two interactive
    shells starting together could both pass the exists() check and both
    prompt/detmux). A second sequential call for the SAME boot loses;
    the stale-marker sweep — moved here from the removed mark_prompted —
    still runs on a win."""
    assert not rescue.already_prompted(tmp_path, "boot-1")
    assert rescue.claim_prompt(tmp_path, "boot-1") is True
    assert rescue.already_prompted(tmp_path, "boot-1")
    assert rescue.claim_prompt(tmp_path, "boot-1") is False  # already claimed

    assert rescue.claim_prompt(tmp_path, "boot-2") is True  # new boot
    assert rescue.already_prompted(tmp_path, "boot-2")
    assert not rescue.already_prompted(tmp_path, "boot-1")  # stale marker swept


def test_claim_prompt_race_exactly_one_winner(tmp_path):
    """The race Task-3 review flagged: N threads (standing in for N shells
    starting together) all call claim_prompt for the same boot_id — the
    O_CREAT|O_EXCL claim must let exactly one through."""
    n = 32
    results: list[bool] = [False] * n
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = rescue.claim_prompt(tmp_path, "boot-race")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1


def test_docs_no_longer_say_shim_wiring_is_pending():
    """Task 3 landed the shim wiring (crr.bash/zsh/fish all call `crr
    rescue-check` on interactive startup) — CHANGELOG.md and DESIGN.md must
    not still claim it's a future task."""
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    design = (_REPO_ROOT / "DESIGN.md").read_text(encoding="utf-8")
    for name, text in (("CHANGELOG.md", changelog), ("DESIGN.md", design)):
        assert "pending a later task" not in text, name
        assert "nothing invokes it yet" not in text, name
