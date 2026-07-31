"""Tests for crr.core.rescue — rescued-session selection + per-boot marker.

A "rescued" session is a journal entry from a PREVIOUS boot whose
conversation the reviver parked in a currently-live tmux session: crashed
shell, revived claude, awaiting re-homing (Phase-3 restore prompt / `crr
rescued`). These tests pin the pure selection rule and the marker
roundtrip/stale-cleanup behavior independent of any adapter.
"""

from crr.core import rescue


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


def test_marker_roundtrip_and_stale_cleanup(tmp_path):
    assert not rescue.already_prompted(tmp_path, "boot-1")
    rescue.mark_prompted(tmp_path, "boot-1")
    assert rescue.already_prompted(tmp_path, "boot-1")
    rescue.mark_prompted(tmp_path, "boot-2")  # new boot
    assert rescue.already_prompted(tmp_path, "boot-2")
    assert not rescue.already_prompted(tmp_path, "boot-1")  # stale marker removed
