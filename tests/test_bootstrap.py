"""bootstrap.sh — the cross-OS one-shot installer.

We cannot exercise the full flow in CI (it installs a package, edits the
user's shell rc, and registers OS service units), so these tests guard the
invariants the flow is only safe with:

  - fail-fast, no-silent-failure bash (``set -euo pipefail``);
  - OS detection for every target (macOS, Linux, and WSL-inside-Linux);
  - it delegates to the existing service installers rather than
    re-implementing them (``crr systemd|launchd|schtasks --install``);
  - the shell shim is installed through a *managed marker block* so a
    re-run replaces it instead of duplicating (idempotency);
  - prerequisites are *detected and confirmed*, never force-installed;
  - the tailnet step is the one that changes what other machines can reach,
    so it must be explained (tailnet-only, loopback otherwise) and gated on
    an explicit ``confirm`` — it must never run silently.

The syntax test needs a real ``bash`` and skips cleanly elsewhere, the same
way the platform adapter tests skip when their tools are absent.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bootstrap.sh"

BASH = shutil.which("bash") or shutil.which("bash.exe")  # windows git-bash


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists_and_is_bash():
    assert SCRIPT.is_file(), "bootstrap.sh is missing from the repo root"
    first = _text().splitlines()[0]
    assert first.startswith("#!") and "bash" in first


@pytest.mark.skipif(BASH is None, reason="bash not available on this platform")
def test_syntax_is_valid_bash():
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"


def test_fail_fast_and_no_unbound_vars():
    # The script must fail loudly: an unset variable or a failed step aborts
    # the run rather than continuing half-configured.
    assert "set -euo pipefail" in _text()


def test_detects_every_target_os():
    t = _text()
    # uname -s values the OS branch keys on.
    assert "Darwin" in t
    assert "Linux" in t
    # WSL is Linux underneath; it must be recognised, not mislabelled.
    assert "WSL_DISTRO_NAME" in t


def test_delegates_to_existing_service_installers():
    # The bootstrap must NOT re-implement the service registration — it
    # drives the same installers the README documents, so the two surfaces
    # can never drift apart.
    t = _text()
    for cmd in ("crr systemd --install", "crr launchd --install", "crr schtasks --install"):
        assert cmd in t, f"bootstrap no longer calls: {cmd}"


def test_shim_install_is_idempotent_marker_block():
    # The shim goes into the rc file between managed markers, so re-running
    # replaces the block instead of appending a second copy.
    t = _text()
    assert "crr shim" in t
    assert "managed by crr bootstrap.sh" in t
    # The replace path reads the block back with awk and re-appends it.
    assert "SHIM_BEGIN" in t and "SHIM_END" in t


def test_prereqs_are_detected_and_confirmed():
    t = _text()
    # Each prerequisite has a suggestion, and installing it goes through the
    # confirm gate (never an unconditional package-manager call).
    assert "ensure_prereq" in t
    assert "suggest_install" in t
    # The confirm gate is the single choke point for risky actions.
    assert "confirm()" in t


def test_tailscale_is_explained_and_consent_gated():
    # The user-facing requirement: the one step that exposes the dashboard to
    # other machines must say what it does (tailnet-only, not public;
    # loopback otherwise) AND require an explicit yes before running.
    t = _text()
    assert "tailscale serve --bg" in t
    assert "tailnet" in t
    assert "loopback" in t
    # The serve command is only reached through a confirm, not run bare.
    assert 'confirm "Expose the dashboard on your tailnet now?"' in t
    assert "run tailscale serve --bg" in t


def test_tailscale_is_offered_installed_and_sign_up_gated():
    # Tailscale is a major component (the dashboard is only remotely useful
    # over it), so the bootstrap must not skip it when missing. It should:
    #   1. OFFER to install tailscale (a suggestion, through the confirm gate —
    #      never a force-install);
    #   2. direct the user to the website to sign up / connect the machine;
    #   3. WAIT for the user to finish that out-of-terminal sign-up before
    #      proceeding — a blocking "press Enter" gate, not an auto-proceed.
    t = _text()
    # 1. Offered for install.
    assert "suggest_install tailscale" in t
    assert 'confirm "Install tailscale now?"' in t
    # 2. Directs the user to the sign-up site. A *missing* tailscale strongly
    #    implies no account yet, so the sign-up URL must be offered in the
    #    manual/deferred fallback too — not just the interactive happy path.
    #    (>=2: the interactive sign-up block + at least one install-failure /
    #    decline fallback.)
    assert t.count("login.tailscale.com") >= 2
    # 3. Blocks until the user says they're done, then re-checks that the
    #    machine is actually connected before serving.
    assert "wait_for" in t
    assert "press Enter to continue" in t
    assert "tailscale ip -4" in t


def test_ends_with_doctor_and_summary():
    # The run must finish by checking its own work (crr doctor) and telling
    # the user what to do next — never silently end after a partial install.
    t = _text()
    assert "crr doctor" in t
    assert "Next steps" in t
