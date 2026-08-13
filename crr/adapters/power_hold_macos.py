"""macOS power hold via caffeinate (implements ports.PowerHolder).

Sleep only. A launch daemon cannot block a macOS shutdown: the
cancellable stage is before ``kLWPointOfNoReturn`` and those
notifications do not reach daemons — only a GUI app in the login session
can delay one, which the spec defers. ``capabilities()`` says so rather
than accepting a shutdown request and silently doing nothing.

``-i`` (idle) and deliberately NOT ``-s``: the lid must keep working.
"""

from __future__ import annotations

import subprocess


def caffeinate_argv() -> list[str]:
    return ["caffeinate", "-i"]


class MacPowerHolder:
    def __init__(self, spawn=None) -> None:
        self._spawn = spawn or (lambda argv, **kw: subprocess.Popen(argv, **kw))
        self._proc = None
        self._held: frozenset[str] = frozenset()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"sleep"})

    def hold(self, want: frozenset[str], reason: str) -> None:
        effective = frozenset(want) & self.capabilities()
        if effective == self._held and self._alive():
            return
        self.release()
        if not effective:
            return
        self._proc = self._spawn(
            caffeinate_argv(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._held = effective

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
