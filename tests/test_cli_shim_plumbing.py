"""CLI-level tests for the shim-support plumbing subcommands added
alongside the shell shims: new-uuid, guess-sid, verify-sid, resume-argv,
take-relaunch-flag, install-shims/uninstall-shims, and service
install/uninstall/status. Run the way shims invoke crr: as a subprocess,
scrubbed env (mirrors test_cli.py's run_cli)."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import crr
from crr import journal
from crr.result import EXIT_NOT_FOUND, EXIT_OK

REPO_ROOT = str(Path(crr.__file__).resolve().parent.parent)


def run_cli(args, state_dir, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CRR_")}
    env["CRR_STATE_DIR"] = str(state_dir)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "crr.cli"] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_new_uuid_prints_a_valid_uuid4(crr_state):
    proc = run_cli(["new-uuid"], crr_state)
    assert proc.returncode == 0
    parsed = uuid.UUID(proc.stdout.strip())
    assert parsed.version == 4
    assert proc.stderr == ""


def test_now_prints_subsecond_precision_epoch(crr_state):
    """The claude() wrapper uses `crr now` (not `date +%s`) to timestamp a
    launch precisely: whole-second resolution can make a picker-guess
    transcript written in the same wall-clock second as the launch look
    "newer" than a truncated launch time and get spuriously verified."""
    import time

    before = time.time()
    proc = run_cli(["now"], crr_state)
    after = time.time()
    assert proc.returncode == 0
    assert proc.stderr == ""
    value = float(proc.stdout.strip())
    assert before - 1 <= value <= after + 1
    # Sub-second resolution: not just an integer with ".0" tacked on.
    assert "." in proc.stdout


def test_guess_sid_empty_when_no_transcripts(crr_state, tmp_path):
    proc = run_cli(
        ["guess-sid", "/nope"], crr_state, extra_env={"CRR_CLAUDE_PROJECTS_DIR": str(tmp_path)}
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_guess_sid_prints_newest_transcript_stem(crr_state, tmp_path):
    cwd = "/home/u/proj"
    proj_dir = tmp_path / cwd.replace("/", "-")
    proj_dir.mkdir(parents=True)
    (proj_dir / "the-sid.jsonl").write_text("{}\n")
    proc = run_cli(
        ["guess-sid", cwd], crr_state, extra_env={"CRR_CLAUDE_PROJECTS_DIR": str(tmp_path)}
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "the-sid"


def test_verify_sid_updates_journal_with_wait_zero(crr_state, tmp_path):
    entry = journal.new_entry(
        pid=42,
        cwd="/home/u/proj",
        shell="bash",
        host="tab",
        boot_id="b",
        claude={"session_id": "guess", "verified": False},
    )
    journal.write_entry(entry)
    proj_dir = tmp_path / "-home-u-proj"
    proj_dir.mkdir(parents=True)
    (proj_dir / "real-sid.jsonl").write_text("{}\n")

    proc = run_cli(
        ["verify-sid", "42", "--started", "0", "--wait", "0"],
        crr_state,
        extra_env={"CRR_CLAUDE_PROJECTS_DIR": str(tmp_path)},
    )
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""

    updated = json.loads((crr_state / "tabs" / "42.json").read_text())
    assert updated["claude"]["session_id"] == "real-sid"
    assert updated["claude"]["verified"] is True


def test_resume_argv_verified_entry(crr_state):
    entry = journal.new_entry(
        pid=7,
        cwd="/w",
        shell="bash",
        host="tab",
        boot_id="b",
        claude={"session_id": "abc-123", "verified": True},
    )
    journal.write_entry(entry)
    proc = run_cli(["resume-argv", "7"], crr_state)
    assert proc.returncode == 0
    assert proc.stdout.splitlines() == ["--resume", "abc-123"]


def test_resume_argv_unverified_entry_falls_back_to_bare_resume(crr_state):
    entry = journal.new_entry(
        pid=8,
        cwd="/w",
        shell="bash",
        host="tab",
        boot_id="b",
        claude={"session_id": "guessed", "verified": False},
    )
    journal.write_entry(entry)
    proc = run_cli(["resume-argv", "8"], crr_state)
    assert proc.returncode == 0
    assert proc.stdout.splitlines() == ["--resume"]


def test_resume_argv_missing_entry(crr_state):
    proc = run_cli(["resume-argv", "424242"], crr_state)
    assert proc.returncode == EXIT_NOT_FOUND
    assert proc.stdout == ""


def test_resume_argv_no_sid_not_found(crr_state):
    entry = journal.new_entry(pid=9, cwd="/w", shell="bash", host="tab", boot_id="b")
    journal.write_entry(entry)
    proc = run_cli(["resume-argv", "9"], crr_state)
    assert proc.returncode == EXIT_NOT_FOUND


def test_take_relaunch_flag_via_cli(crr_state):
    proc = run_cli(["take-relaunch-flag", "55"], crr_state)
    assert proc.returncode == EXIT_NOT_FOUND
    assert proc.stdout == "" and proc.stderr == ""

    journal.write_relaunch_flag(55)
    proc = run_cli(["take-relaunch-flag", "55"], crr_state)
    assert proc.returncode == EXIT_OK
    assert proc.stdout == "" and proc.stderr == ""

    # Consumed: second check finds nothing.
    proc = run_cli(["take-relaunch-flag", "55"], crr_state)
    assert proc.returncode == EXIT_NOT_FOUND


def test_plumbing_commands_never_leak_tracebacks(crr_state, monkeypatch):
    """[lesson: PATH poisoning] every plumbing command must stay silent on
    unexpected internal failure -- exit code only, no stderr noise."""
    from crr import cli

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cli.sidverify, "guess_sid", boom)
    code = cli.main(["guess-sid", "/whatever"])
    assert code == 1


def test_install_shims_cli_writes_files_and_rc(tmp_path, crr_state, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    proc = run_cli(
        ["install-shims", "--shell", "bash"],
        crr_state,
        extra_env={"CRR_BIN": "/abs/path/crr", "HOME": str(home)},
    )
    assert proc.returncode == 0
    assert (crr_state / "shims" / "crr.bash").exists()
    assert (home / ".bashrc").exists()
    assert "bash:" in proc.stdout


def test_uninstall_shims_cli_removes_rc_lines(tmp_path, crr_state):
    home = tmp_path / "home"
    home.mkdir()
    run_cli(
        ["install-shims", "--shell", "bash"],
        crr_state,
        extra_env={"CRR_BIN": "/abs/path/crr", "HOME": str(home)},
    )
    proc = run_cli(
        ["uninstall-shims", "--shell", "bash"], crr_state, extra_env={"HOME": str(home)}
    )
    assert proc.returncode == 0
    text = (home / ".bashrc").read_text()
    assert "crr shim" not in text


def test_install_shims_cli_no_shells_found(crr_state, monkeypatch):
    proc = run_cli(
        ["install-shims"],
        crr_state,
        extra_env={"PATH": "/nonexistent-empty-dir"},
    )
    assert proc.returncode != 0
    assert "no supported shell" in proc.stderr


def test_service_status_cli_runs(crr_state):
    proc = run_cli(["service", "status"], crr_state)
    assert proc.returncode == 0
    for name in ("crr-web.service", "crr-watchdog.service", "crr-watchdog.timer"):
        assert name in proc.stdout
