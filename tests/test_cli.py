import json
import os
import subprocess
import sys
from pathlib import Path

import crr
from crr import cli, journal
from crr.result import EXIT_NOT_FOUND, EXIT_REFUSED

REPO_ROOT = str(Path(crr.__file__).resolve().parent.parent)


def run_cli(args, state_dir):
    """Run the CLI the way shims will: as a subprocess, scrubbed env."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CRR_")}
    env["CRR_STATE_DIR"] = str(state_dir)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "crr.cli"] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_register_status_update_deregister_flow(crr_state):
    proc = run_cli(
        ["register", "--pid", "321", "--cwd", "/tmp/x", "--shell", "fish",
         "--host", "tmux"],
        crr_state,
    )
    assert proc.returncode == 0
    # Plumbing is silent: no stdout, no stderr noise (runs from shell hooks).
    assert proc.stdout == ""
    assert proc.stderr == ""

    proc = run_cli(["status", "--json"], crr_state)
    assert proc.returncode == 0
    items = json.loads(proc.stdout)
    assert len(items) == 1
    assert items[0]["pid"] == 321
    assert items[0]["shell"] == "fish"
    assert items[0]["host"] == "tmux"
    assert items[0]["state"] in ("live", "ghost", "crashed")

    proc = run_cli(
        ["update", "321", "--last-cmd", "vim notes.md", "--claude-sid",
         "11111111-2222-3333-4444-555555555555", "--sid-verified", "false"],
        crr_state,
    )
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""

    proc = run_cli(["status", "--json"], crr_state)
    items = json.loads(proc.stdout)
    assert items[0]["last_cmd"] == "vim notes.md"
    assert items[0]["claude"]["session_id"] == "11111111-2222-3333-4444-555555555555"
    assert items[0]["claude"]["verified"] is False

    proc = run_cli(["deregister", "321"], crr_state)
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""

    proc = run_cli(["status", "--json"], crr_state)
    assert json.loads(proc.stdout) == []


def test_deregister_missing_is_silent_success(crr_state):
    proc = run_cli(["deregister", "424242"], crr_state)
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""


def test_update_missing_entry_silent_nonzero(crr_state):
    proc = run_cli(["update", "424242", "--cwd", "/x"], crr_state)
    assert proc.returncode == EXIT_NOT_FOUND
    assert proc.stdout == "" and proc.stderr == ""


def test_status_human_output_empty(crr_state):
    proc = run_cli(["status"], crr_state)
    assert proc.returncode == 0
    assert "no sessions journaled" in proc.stdout


def test_remove_missing_exit_code(crr_state):
    proc = run_cli(["remove", "424242"], crr_state)
    assert proc.returncode == EXIT_NOT_FOUND
    assert "not-found" in proc.stdout


def test_kick_refused_on_crashed_via_cli(crr_state):
    """End-to-end recycled-pid gate through the CLI."""
    entry = journal.new_entry(
        pid=os.getpid(), cwd="/w", shell="bash", host="tab",
        boot_id="boot-from-a-previous-life",
    )
    journal.write_entry(entry)
    proc = run_cli(["kick", str(os.getpid())], crr_state)
    assert proc.returncode == EXIT_REFUSED
    assert "refused-crashed" in proc.stdout


def test_diagnose_stub_reports_per_source(crr_state):
    proc = run_cli(["diagnose"], crr_state)
    assert proc.returncode == 0
    for source in ("boots", "prev_boot_errors", "host_events"):
        assert "%s: not yet implemented" % source in proc.stdout


def test_gc_runs_clean(crr_state):
    proc = run_cli(["gc"], crr_state)
    assert proc.returncode == 0
    assert proc.stdout.startswith("gc:")


def test_revive_nothing_to_do(crr_state):
    proc = run_cli(["revive", "--all"], crr_state)
    assert proc.returncode == 0
    assert "nothing to revive" in proc.stdout


def test_main_callable_in_process(crr_state, capsys):
    # The console entry point (crr = crr.cli:main) is this function.
    code = cli.main(["status", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out) == []


def test_plumbing_never_raises_to_hooks(crr_state, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cli.journal, "write_entry", boom)
    code = cli.main(
        ["register", "--pid", "1", "--cwd", "/x", "--shell", "zsh",
         "--host", "tab"]
    )
    assert code == 1  # failure propagates as an exit code...
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""  # ...silently
