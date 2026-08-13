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

**That setting is read from the EFFECTIVE config, not from one file.**
logind's recommended override mechanism is a drop-in
(``{/etc,/run,/usr/lib}/systemd/logind.conf.d/*.conf``), not an edit to
``logind.conf``, and on Fedora-likes ``/usr/lib/systemd/logind.conf`` is
the only main file that exists. Reading ``/etc/systemd/logind.conf`` alone
means a host that turned the exemption off in a drop-in reads back as the
compiled-in ``yes`` -- crr holds ``sleep`` and closing the lid stops
suspending the machine, which is the one outcome this design must never
produce.

The rule here is deliberately NOT logind's precedence algorithm: **if any
source says a falsey value, withhold sleep.** That is precedence-proof
without implementing precedence, and it errs toward "do not touch the
lid". ``systemd-analyze cat-config`` would give the truly effective
config, but this runs on a poll path and shelling out there is what the
rest of these adapters exist to avoid.

Three-way, mirroring ``power_source.SysfsPowerSource.on_ac()``:

- No config source exists at all -> logind's compiled-in default (``yes``)
  applies. That is a KNOWN True, not an unknown.
- A source exists but cannot be read (or a drop-in directory cannot even be
  listed) -> unknown, ``None``. Never a positive "safe to hold".
- Otherwise the parsed answer.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from crr.adapters._proc import (FORCE_WAIT_SECONDS, RELEASE_WAIT_SECONDS,
                                release_child, signal_child)

# Relative on purpose: joined onto an injectable root so the whole set is
# testable against a fake filesystem. `root / "/etc/..."` would silently
# discard root and read the real host.
LOGIND_MAIN = (
    "etc/systemd/logind.conf",
    # The only main file on Fedora-likes; absent on Debian-likes.
    "usr/lib/systemd/logind.conf",
)
LOGIND_DROPIN_DIRS = (
    "etc/systemd/logind.conf.d",
    "run/systemd/logind.conf.d",
    "usr/lib/systemd/logind.conf.d",
)

_LID_RE = re.compile(
    r"^\s*LidSwitchIgnoreInhibited\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE
)
_FALSEY = ("no", "false", "0", "off")


def lid_is_exempt(conf_text: str) -> bool:
    """True when closing the lid sleeps even while an inhibitor is held.

    The pure single-text parser. Defaults to True because logind's own
    default is ``yes``; a commented line is not a setting, and ``^\\s*``
    under ``re.MULTILINE`` already cannot match ``#LidSwitch...``, so
    ``match is None`` covers the commented case with no extra guard.
    """
    match = None
    for candidate in _LID_RE.finditer(conf_text):
        match = candidate
    if match is None:
        return True
    return match.group(1).strip().lower() not in _FALSEY


def logind_sources(root: Path) -> tuple[list[Path], bool]:
    """Every file logind would read under ``root``, and whether the walk
    was complete.

    Returns ``(candidate_paths, complete)``. The main files are returned as
    candidates whether or not they exist -- the reader discriminates
    "absent" from "unreadable", which a bare ``list[Path]`` cannot carry.
    ``complete`` is False when a drop-in directory exists but could not be
    listed: an unlistable directory is unknown, and returning it as empty
    would be the same defect this whole function fixes.
    """
    root = Path(root)
    paths = [root / rel for rel in LOGIND_MAIN]
    complete = True
    for rel in LOGIND_DROPIN_DIRS:
        try:
            entries = sorted((root / rel).iterdir())
        except FileNotFoundError:
            continue                      # no such drop-in dir: not a source
        except OSError:
            complete = False              # it exists; we just cannot see in
            continue
        paths.extend(e for e in entries if e.name.endswith(".conf"))
    return paths, complete


def lid_exemption(root: Path) -> bool | None:
    """Three-way lid exemption across every logind config source.

    ``True`` exempt (a sleep inhibitor leaves the lid alone), ``False`` not
    exempt (a sleep inhibitor WOULD block the lid), ``None`` unknown.
    """
    paths, complete = logind_sources(root)
    unknown = not complete
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue                      # not a source on this distro
        except OSError:
            unknown = True                # exists, unreadable: not a "yes"
            continue
        if not lid_is_exempt(text):
            # A definite falsey value anywhere wins outright, so the answer
            # never depends on precedence -- and it beats an unreadable
            # sibling, because False and None both withhold anyway.
            return False
    if unknown:
        return None
    # Either every source is silent on the key or there are no sources at
    # all; both land on logind's compiled-in default, which is `yes`.
    return True


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

    def __init__(self, conf_root: Path | None = None, spawn=None) -> None:
        # The filesystem root the logind config is read from. Injectable so
        # the whole source set (main files AND drop-in dirs) is testable.
        self._root = Path("/") if conf_root is None else Path(conf_root)
        self._spawn = spawn or (lambda argv, **kw: subprocess.Popen(argv, **kw))
        self._proc = None
        self._held: frozenset[str] = frozenset()
        self._withheld: str | None = None

    def capabilities(self) -> frozenset[str]:
        return frozenset({"sleep", "shutdown"})

    def withheld(self) -> str | None:
        """Why part of the request was dropped, for doctor."""
        return self._withheld

    def hold(self, want: frozenset[str], reason: str) -> None:
        self._withheld = None
        effective = set(want)
        if "sleep" in effective:
            exempt = lid_exemption(self._root)
            if exempt is not True:
                effective.discard("sleep")
                # Two different withholdings need two different reasons:
                # saying "this host sets LidSwitchIgnoreInhibited=no" when
                # the config could not be read asserts a fact never
                # established.
                self._withheld = (
                    "not blocking sleep: this host sets "
                    "LidSwitchIgnoreInhibited=no (in logind.conf or a "
                    "drop-in), so a sleep inhibitor would also block "
                    "closing the lid"
                    if exempt is False else
                    "not blocking sleep: could not read this host's logind "
                    "configuration, so whether a sleep inhibitor would also "
                    "block closing the lid is unknown"
                )
        effective_fs = frozenset(effective)
        if effective_fs == self._held and self._alive():
            return
        self.release()
        if not effective_fs:
            return
        # stderr is CAPTURED, not discarded. systemd-inhibit exits nonzero
        # in milliseconds on a host with no logind session or a polkit
        # denial (measured on this WSL box, 2026-08-13: "Failed to inhibit:
        # Access denied", exit 1), and with DEVNULL the one line that
        # explains why was destroyed at the source -- held() went full set,
        # then empty, with withheld() None and no reason recorded anywhere.
        self._proc = self._spawn(
            inhibit_argv(effective_fs, reason),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._held = effective_fs
        # Poll once: a denied inhibit is usually already gone. It often is
        # not scheduled yet either, which is why held() reaps too rather
        # than trusting this one call.
        self._reap_if_dead()

    def _reap_if_dead(self) -> None:
        """Drop the claim (and record why) if the child has already exited."""
        proc = self._proc
        if proc is None or proc.poll() is None:
            return
        detail = self._drain_stderr(proc)
        code = proc.returncode
        if code:
            self._withheld = (
                f"systemd-inhibit exited {code} without holding anything"
                + (f": {detail}" if detail else "")
            )
        # Drop the handle so this runs exactly ONCE. It is called from both
        # hold() and every held(), and a second pass would read a stderr
        # stream it already closed -- overwriting the recorded reason with
        # a detail-free one, i.e. destroying the explanation a second time.
        # The child is exited and already reaped by poll(); there is
        # nothing left to do with it.
        self._proc = None
        self._held = frozenset()

    @staticmethod
    def _drain_stderr(proc) -> str:
        """Read stderr from an EXITED child. Never call this on a live one:
        it would block the poll path forever."""
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return ""
        try:
            raw = stream.read() or b""
        except Exception:
            return ""
        finally:
            try:
                stream.close()          # or an fd leaks per hold/release
            except Exception:
                pass
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return " ".join(raw.split())

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def held(self) -> frozenset[str]:
        self._reap_if_dead()
        return self._held if self._alive() else frozenset()

    def release(self) -> None:
        """Signal, then escalate until the child is CONFIRMED reaped.

        The old version called ``terminate()`` then ``wait(5)`` ONCE and
        cleared ``_proc``/``_held`` unconditionally -- even when the wait
        raised. A child that ignores SIGTERM left crr with no handle to a
        process that may still hold ``systemd-inhibit``'s lock,
        permanently uncleanable, ``held()`` reporting nothing held. The
        same defect was fixed on the Windows side; this shares that fix's
        ladder (``crr.adapters._proc.release_child``) rather than keeping
        two copies that would only drift apart again.

        There is no stdin to close here (unlike the Windows holder):
        ``terminate()`` IS the graceful request, sent up front so the
        ladder's own first wait is checking whether that already worked
        rather than waiting out the whole first-wait budget for nothing.

        The handle is dropped only on confirmation. Deliberately no
        ``finally:`` around the bookkeeping -- that is exactly the shape
        that reinstates the bug.
        """
        proc = self._proc
        if proc is None:
            self._held = frozenset()
            return
        signal_child(proc, "terminate")
        if not release_child(proc, RELEASE_WAIT_SECONDS, FORCE_WAIT_SECONDS):
            # Neither the initial terminate nor the escalation to kill
            # confirmed it dead. KEEP the handle and KEEP reporting the
            # set: a live child may genuinely still be holding, and the
            # next hold() retries release(). Reporting an empty hold here
            # would be the same lie, just quieter.
            return
        # Gated on a CONFIRMED exit. `stream.read()` on a live child's
        # pipe does not raise -- it blocks until EOF, i.e. forever,
        # wedging the poll loop. An unreaped child's fd is closed by
        # the OS when the Popen object is dropped, so skipping the
        # drain here costs nothing.
        self._drain_stderr(proc)
        self._proc = None
        self._held = frozenset()
