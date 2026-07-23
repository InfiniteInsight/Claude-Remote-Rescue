"""Shared helpers for shell-shim contract tests.

Spawns a real shell (zsh/bash/fish) attached to a pty (so job control
behaves like a real terminal and no "no job control in this shell"
noise pollutes the transcript), feeds it a scripted sequence of
commands, and captures everything the shell wrote back.

The dev crr entry point mirrors what `test_cli.py`'s ``run_cli`` does
(subprocess into this worktree's ``crr`` package) but as a real,
directly-executable script so it can be used as ``CRR_BIN`` -- exactly
how an installed shim invokes crr.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import subprocess
import termios
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIMS_DIR = REPO_ROOT / "shims"


def shell_available(name: str) -> bool:
    return shutil.which(name) is not None


def make_dev_crr_bin(tmp_path: Path) -> Path:
    """A real, executable ``crr`` entry point that always resolves this
    worktree's ``crr`` package, regardless of caller cwd -- usable
    directly as ``CRR_BIN``."""
    path = tmp_path / "dev-crr-bin"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from crr.cli import main\n"
        "sys.exit(main())\n" % str(REPO_ROOT)
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_fake_claude(tmp_path: Path, log_path: Path, sleep_seconds: float = 0.3) -> Path:
    """A tiny fake ``claude`` executable: records its argv (and, on
    request, its environment) to *log_path*, sleeps briefly, exits 0."""
    path = tmp_path / "claude"
    path.write_text(
        "#!/usr/bin/env bash\n"
        'exec >> %s\n'
        'echo "ARGV: $*"\n'
        'env | sort | sed -n \'/^CRR_/p\'\n'
        'echo "---END---"\n'
        "sleep %s\n"
        "exit 0\n" % (_sh_quote(str(log_path)), sleep_seconds)
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _preexec_new_session():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll *predicate* (a zero-arg callable) until it's truthy or
    *timeout* elapses. Returns the last truthy/falsy result."""
    deadline = time.time() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.time() >= deadline:
            return result
        time.sleep(interval)


class ShellSession:
    """A real interactive shell attached to a pty.

    Feed it commands with ``send``; use ``wait_for`` (module-level) to
    poll journal state rather than guessing sleep durations. ``close``
    sends `exit` and waits for the process to actually terminate.
    """

    def __init__(self, argv: List[str], env: Dict[str, str]):
        self.master, slave = os.openpty()
        self.proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            preexec_fn=_preexec_new_session,
            close_fds=True,
        )
        os.close(slave)
        self._closed = False

    def send(self, command: str, settle: float = 0.3) -> None:
        os.write(self.master, (command + "\n").encode())
        if settle:
            time.sleep(settle)

    def close(self, timeout: float = 10.0) -> str:
        """Send `exit`, wait for the shell to terminate, and return all
        captured output."""
        if not self._closed:
            try:
                os.write(self.master, b"exit\n")
            except OSError:
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self._closed = True
        output = b""
        try:
            while True:
                chunk = os.read(self.master, 65536)
                if not chunk:
                    break
                output += chunk
        except OSError:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        return output.decode(errors="replace")

    def kill(self) -> None:
        if not self._closed:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self._closed = True
            try:
                os.close(self.master)
            except OSError:
                pass


def run_shell_session(
    argv: List[str],
    env: Dict[str, str],
    commands: List[str],
    settle: float = 0.35,
    timeout: float = 15.0,
) -> str:
    """Run *argv* (an interactive shell) attached to a pty, feed it
    *commands* one at a time (each followed by a short settle delay so
    hooks / background jobs have a chance to run), then wait for exit.

    Returns everything the shell wrote back, decoded best-effort.
    """
    session = ShellSession(argv, env)
    for cmd in commands:
        session.send(cmd, settle=settle)
    return session.close(timeout=timeout)


def base_env(state_dir: Path, crr_bin: Path, extra_path: Optional[Path] = None) -> Dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("CRR_"):
            del env[key]
    env["CRR_STATE_DIR"] = str(state_dir)
    env["CRR_BIN"] = str(crr_bin)
    env["TERM"] = "dumb"
    env["HOME"] = str(state_dir.parent)  # keep rc-file / home-dir logic sandboxed
    if extra_path is not None:
        env["PATH"] = str(extra_path) + os.pathsep + env.get("PATH", "")
    return env
