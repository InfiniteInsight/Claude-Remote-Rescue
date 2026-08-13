"""Linux power hold via systemd-inhibit (implements ports.PowerHolder).

**It is `--what=sleep`, not `--what=idle`.** This is the opposite of the
obvious choice and an earlier draft of the design got it backwards, so the
reasoning lives here rather than in a commit message. Per logind.conf(5):

- ``IdleAction=`` defaults to ``ignore``. An ``idle`` lock therefore
  inhibits a mechanism that is switched off on a default system: it would
  hold successfully, report success, and protect nothing.
- ``LidSwitchIgnoreInhibited=`` defaults to ``yes``. A ``sleep`` lock
  therefore does NOT block closing the lid, which is the builder's hard
  requirement.

GNOME and KDE suspend on idle by asking logind to ``Suspend()`` as an
unprivileged user, and that is exactly what a ``sleep`` lock inhibits.

The one case that breaks this: a host that has set
``LidSwitchIgnoreInhibited=no``. There a sleep lock WOULD block the lid, so
``hold()`` withholds the sleep half and says so rather than quietly
violating the requirement.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

LOGIND_CONF = Path("/etc/systemd/logind.conf")

_LID_RE = re.compile(
    r"^\s*LidSwitchIgnoreInhibited\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE
)
_FALSEY = ("no", "false", "0", "off")


def lid_is_exempt(conf_text: str) -> bool:
    """True when closing the lid sleeps even while an inhibitor is held.

    Defaults to True because logind's own default is ``yes``; a commented
    line is not a setting, so the regex deliberately anchors on a line that
    does not start with ``#``.
    """
    match = None
    for candidate in _LID_RE.finditer(conf_text):
        line = conf_text[:candidate.start()].split("\n")[-1]
        if line.lstrip().startswith("#"):
            continue
        match = candidate
    if match is None:
        return True
    return match.group(1).strip().lower() not in _FALSEY


def inhibit_argv(want: frozenset[str], reason: str) -> list[str]:
    """The systemd-inhibit command line for ``want``."""
    return [
        "systemd-inhibit",
        "--what", ":".join(sorted(want)),
        "--mode", "block",
        "--who", "crr",
        "--why", reason,
        # Sleeps forever; the hold ends when this child is killed, which is
        # the whole crash-safety property.
        "sleep", "infinity",
    ]


class LinuxPowerHolder:
    """Holds via a systemd-inhibit child process."""

    def __init__(self, logind_conf: Path | None = None, spawn=None) -> None:
        self._conf = LOGIND_CONF if logind_conf is None else Path(logind_conf)
        self._spawn = spawn or (lambda argv, **kw: subprocess.Popen(argv, **kw))
        self._proc = None
        self._held: frozenset[str] = frozenset()
        self._withheld: str | None = None

    def capabilities(self) -> frozenset[str]:
        return frozenset({"sleep", "shutdown"})

    def withheld(self) -> str | None:
        """Why part of the request was dropped, for doctor."""
        return self._withheld

    def _lid_exempt(self) -> bool:
        try:
            text = self._conf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True  # logind's own default
        return lid_is_exempt(text)

    def hold(self, want: frozenset[str], reason: str) -> None:
        self._withheld = None
        effective = set(want)
        if "sleep" in effective and not self._lid_exempt():
            effective.discard("sleep")
            self._withheld = (
                "not blocking sleep: this host sets "
                "LidSwitchIgnoreInhibited=no, so a sleep inhibitor would "
                "also block closing the lid"
            )
        effective_fs = frozenset(effective)
        if effective_fs == self._held and self._alive():
            return
        self.release()
        if not effective_fs:
            return
        self._proc = self._spawn(
            inhibit_argv(effective_fs, reason),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._held = effective_fs

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def held(self) -> frozenset[str]:
        return self._held if self._alive() else frozenset()

    def release(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        self._held = frozenset()
