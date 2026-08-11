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
