"""Suite-wide fixtures.

Currently one: a boot-identity stand-in for platforms crr has no adapter
for. See ``_boot_identity_on_unsupported_platforms`` for why that is a test
seam rather than a cover-up.
"""

import platform

import pytest

from crr.adapters import boot_identity


def set_home(monkeypatch, path) -> None:
    """Point home-directory lookups at ``path``, on any platform.

    ``monkeypatch.setenv("HOME", ...)`` is a no-op on Windows:
    ``ntpath.expanduser`` — which is what ``Path.home()`` ends up in —
    consults ``USERPROFILE`` first and ``HOMEDRIVE``/``HOMEPATH`` after,
    and never looks at ``HOME`` at all. So every test that redirected home
    that way was quietly reading the real user profile there, finding no
    seeded transcripts, and failing on an assertion about discovery rather
    than about the environment it thought it had built.

    Both are set rather than branching: the shims read ``$HOME`` directly,
    so a Windows-only ``USERPROFILE`` would drift from what they see.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


# Any string works; it only has to be stable within a run, because that is
# the entire contract the tests downstream of it depend on (an entry
# journaled this boot matches, one from a prior boot does not).
_FAKE_BOOT_ID = "conftest-boot-id-0000"


class _FakeBootIdentity:
    def current(self) -> str:
        return _FAKE_BOOT_ID


@pytest.fixture(autouse=True)
def _boot_identity_on_unsupported_platforms(request, monkeypatch):
    """Stand in for ``boot_identity.detect()`` where crr has no adapter.

    ``detect()`` raises ``NotImplementedError`` on anything that is not
    Linux or macOS, and it is reached from ~20 call sites in ``crr.cli`` —
    so on Windows every command exits 2 before it does anything, and 20
    tests fail with the same message about a seam none of them are about.
    ``test_cmd_reopen_stays_quiet_when_the_tab_opened`` already stubs the
    state dir, the tab spawner, ``ops.reopen`` and ``RealTmux``; boot
    identity is the one platform seam it forgot, because on POSIX it
    happened to work. Stubbing it here finishes that list.

    **This fixture does not claim crr works on the platform.** It cannot,
    and the claim would be false today (#75). What holds the honest line is
    ``test_boot_identity.py``, which opts out via the ``real_boot_identity``
    marker and asserts the raise directly. If that ever became a stub too,
    the gap would go silent — which is the whole failure mode this comment
    exists to prevent.
    """
    if "real_boot_identity" in request.keywords:
        return
    if platform.system() in ("Linux", "Darwin"):
        return
    monkeypatch.setattr(boot_identity, "detect", lambda: _FakeBootIdentity())


@pytest.fixture(autouse=True)
def _forbid_unstubbed_exec(monkeypatch):
    """Make a reached-but-unstubbed exec seam fail loudly, not silently.

    ``crr.cli._exec`` is ``os.execvp``: reaching it replaces the running
    process. In a test that means the pytest process itself is overwritten —
    no traceback, no failure summary, the suite just stops mid-run with the
    remaining tests never reported. That is precisely how the Ubuntu CI job
    went dark for days (a rescue-check test drove the real reopen path with
    the seam live). Defaulting the seam to a raise converts that whole failure
    class into an ordinary, named test failure.

    Tests that genuinely exercise exec override this with their own
    ``monkeypatch.setattr(cli, "_exec", ...)``; that runs after this fixture
    and wins for the duration of the test.
    """
    from crr import cli

    def _forbidden(*args, **kwargs):
        raise RuntimeError(
            "cli._exec (os.execvp) reached without being stubbed: this test "
            "drove a real exec path and would have replaced the pytest process. "
            "Stub cli._exec in the test (see the rescue-check tests for the "
            "pattern)."
        )

    monkeypatch.setattr(cli, "_exec", _forbidden)


@pytest.fixture(autouse=True)
def _forbid_unstubbed_tab_spawn(monkeypatch):
    """Default the tab-spawn adapters' subprocess seam to a loud raise.

    A test that reaches a REAL tab spawn opens an actual Windows Terminal /
    desktop terminal tab on the developer's machine — one leaked tab per
    suite run (the tab outlives its dead `tmux attach` because WT's default
    closeOnExit=graceful keeps nonzero-exit tabs open). That is exactly how
    `crr reopen`'s e2e test papered the user's terminal with dead tabs
    (bug 2026-09-03). Same failure class and same remedy as
    `_forbid_unstubbed_exec` above: convert the escape into a named failure.

    Each adapter module's ``subprocess`` NAME is rebound to a per-module
    shim — never ``subprocess.run`` on the global module object, which every
    test file shares (the tmux e2e tests shell out for real). Tab-spawn unit
    tests keep working unchanged: their ``monkeypatch.setattr(tsw.subprocess,
    "run", ...)`` now lands on the shim instance, replacing the raise for the
    duration of the test. The exception/type attributes the adapters name in
    ``except`` clauses are forwarded so those clauses still resolve.
    """
    import subprocess as _real_subprocess

    from crr.adapters import tab_spawn, tab_spawn_linux, tab_spawn_windows

    class _NoSpawnSubprocess:
        TimeoutExpired = _real_subprocess.TimeoutExpired
        CalledProcessError = _real_subprocess.CalledProcessError
        CompletedProcess = _real_subprocess.CompletedProcess

        def run(self, *args, **kwargs):
            raise RuntimeError(
                "a tab-spawn adapter reached subprocess.run without being "
                "stubbed: this test would open a real terminal tab. Stub the "
                "spawner (monkeypatch cli._tab_spawner, or the adapter "
                "module's subprocess.run — see tests/test_tab_spawn_windows.py)."
            )

    for module in (tab_spawn, tab_spawn_linux, tab_spawn_windows):
        monkeypatch.setattr(module, "subprocess", _NoSpawnSubprocess())
