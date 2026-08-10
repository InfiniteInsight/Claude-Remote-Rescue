"""End-to-end smoke of the Phase-1 Linux happy path — fully isolated.

Exercises register → claude-launch → crash → revive → status → web against
the REAL cli/reviver/tmux, with zero risk to anything real:

- a scratch ``XDG_STATE_HOME`` (never the user's state dir),
- a scratch ``HOME`` (never the user's ~/.claude transcripts),
- a scratch tmux server via ``TMUX_TMPDIR`` (killed in teardown),
- a throwaway ``sleep`` process as the "shell" pid (not a real session),
- a fake ``claude`` on PATH (so the revived tmux pane has something to run),
- an OS-assigned loopback port (never 8377 / the user's dashboard).

Gated to Linux + tmux; this is the local stand-in for the author-run
hardware acceptance test (minus the actual reboot).
"""

import json
import os
import platform
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from crr import cli
from crr.adapters import state_dir
from crr.core import contracts
from crr.core.journal import JournalStore

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux" or shutil.which("tmux") is None,
    reason="e2e smoke needs Linux + tmux",
)


def _fake_claude(bindir):
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "claude"
    # Word-form `claude --resume <sid>` must exec something that stays alive so
    # the revived tmux session persists long enough to observe.
    fake.write_text("#!/usr/bin/env bash\nexec sleep 300\n", encoding="utf-8")
    fake.chmod(0o755)
    return bindir


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    scratch = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    bindir = _fake_claude(tmp_path / "bin")
    monkeypatch.setattr(state_dir, "state_dir", lambda: scratch)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(scratch.parent))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "tmux"))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    (tmp_path / "tmux").mkdir()
    yield scratch
    subprocess.run(["tmux", "kill-server"], capture_output=True,
                   env={**os.environ, "TMUX_TMPDIR": str(tmp_path / "tmux")})


def test_register_launch_crash_revive_status_web(isolated, tmp_path, capsys):
    scratch = isolated
    # 1. A throwaway process stands in for a shell that will "crash".
    sleeper = subprocess.Popen(["sleep", "300"])
    pid = sleeper.pid
    try:
        # 2. Register the shell + launch a claude session (injected sid).
        assert cli.main(["register", "--pid", str(pid), "--cwd", str(tmp_path),
                         "--shell", "bash", "--host", "tmux"]) == 0
        capsys.readouterr()
        assert cli.main(["claude-launch", "--pid", str(pid)]) == 0
        sid = capsys.readouterr().out.strip()
        assert len(sid) == 36  # a real uuid was journaled

        # 3. Crash: kill the shell pid so it classifies `crashed`.
        sleeper.send_signal(signal.SIGKILL)
        sleeper.wait()
        time.sleep(0.1)

        # 4. Revive: the reviver spawns `claude --resume <sid>` into tmux.
        assert cli.main(["revive"]) == 0
        capsys.readouterr()

        name = f"crr-{sid[:8]}"
        listed = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
            env={**os.environ, "TMUX_TMPDIR": str(tmp_path / "tmux")},
        )
        assert name in listed.stdout, f"revived tmux session missing: {listed.stdout!r}"
        # Journal now records the tmux session for that entry.
        assert JournalStore(scratch).read(pid)["tmux_session"] == name

        # 5. status --json emits a contract-valid payload with the card.
        assert cli.main(["status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        contracts.validate_sessions_payload(payload)
        card = next(c for c in payload["sessions"] if c["pid"] == pid)
        assert card["session_id"] == sid
        # The journaled shell pid is dead, so `classify()` still returns
        # CRASHED — which `ops.detmux`/`ops.untmux` require. But step 4 just
        # revived the conversation into a live tmux session, so the CARD
        # reads `parked` (spec 2026-08-09, Phase 0). This is the only test in
        # the suite with a real tmux server, so it is the end-to-end proof
        # that `cli._live_tmux_sessions` reaches the display projection.
        assert card["state"] == "parked"

        # 6. Serve the dashboard on an OS-assigned port and hit both APIs.
        config = cli._load_config()
        handler = cli.make_web_handler(
            lambda: cli.status.assemble_sessions(
                JournalStore(scratch).scan().entries,
                cli.boot_identity.detect(),
                cli.process_probe.PsProcessProbe(config.get("interop_timeout_seconds")),
            ),
            {"localhost", "127.0.0.1"}, (".ts.net",),
            diagnostics_provider=lambda: cli.gather_diagnostics(config),
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        assert port != 8377  # never the user's real dashboard port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sessions",
                                         headers={"Host": "localhost"})
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 200
                body = json.loads(r.read())
                contracts.validate_sessions_payload(body)
                assert any(c["pid"] == pid for c in body["sessions"])

            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/diagnostics",
                                         headers={"Host": "localhost"})
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
                diag = json.loads(r.read())
                contracts.validate_diagnostics_payload(diag)
                assert diag["summary"]  # the plain-English verdict is present
        finally:
            server.shutdown()
            server.server_close()
    finally:
        if sleeper.poll() is None:
            sleeper.send_signal(signal.SIGKILL)
            sleeper.wait()
