"""PAGE_VERSION must move whenever page.html does (#59).

`PAGE_VERSION` is the cache-invalidation token for a long-lived dashboard:
the served page re-checks `/api/version` every 30s and reloads itself when
the number differs from the one baked into it. A page change that ships
WITHOUT a bump means any client already displaying that number never
reloads — stale JavaScript against current server code, indefinitely.

Until this test, the only thing enforcing the bump was a comment. Two
branches collided on the number twice in two days; both were caught by git
(same line, so the merge conflicts) rather than by anything deliberate.
The variant git cannot catch is one branch bumping while another edits the
page without bumping: different files, different lines, clean merge, two
page changes under one version.

The pin below is the enforcement. Change `page.html` and this test fails
until you bump `PAGE_VERSION` and add a new entry — the same shape as
`test_version_ledger.py`, which does this for the contract constants.
"""

import hashlib
from pathlib import Path

import pytest

from crr.core import web

_PAGE = Path(__file__).resolve().parent.parent / "crr" / "core" / "page.html"


def _page_sha() -> str:
    return hashlib.sha256(_PAGE.read_bytes()).hexdigest()


# version -> sha256 of crr/core/page.html when that version shipped.
# APPEND a new entry for every page change; never edit an existing one.
PAGE_PINS: dict[int, str] = {
    45: "f18d2516203d2868d60452cf478bc19dd3b36f802f9df066c77b40e17bf2d7e9",
    46: "65abf9908a2d494db3275a61fc666faae197bc3a6c790342e02325223979f817",
}


def test_page_html_matches_the_pin_for_the_current_version():
    assert web.PAGE_VERSION in PAGE_PINS, (
        f"PAGE_VERSION is {web.PAGE_VERSION} with no pin entry. Add "
        f'{web.PAGE_VERSION}: "{_page_sha()}" to PAGE_PINS.'
    )
    assert PAGE_PINS[web.PAGE_VERSION] == _page_sha(), (
        f"crr/core/page.html changed while PAGE_VERSION stayed at "
        f"{web.PAGE_VERSION}. Every open dashboard compares that number to "
        f"decide whether to reload, so reusing it strands clients on stale "
        f"JavaScript. Bump PAGE_VERSION and APPEND a new pin entry:\n"
        f'    {web.PAGE_VERSION + 1}: "{_page_sha()}",'
    )


def test_no_two_versions_share_a_page_hash():
    """Catches a copy-pasted entry — a new version pinned to the old page,
    which would let the next real page change slip through unnoticed."""
    seen: dict[str, int] = {}
    for version, sha in PAGE_PINS.items():
        assert sha not in seen, (
            f"versions {seen[sha]} and {version} pin the same page hash; one "
            "of them was copy-pasted rather than measured"
        )
        seen[sha] = version


@pytest.mark.parametrize("bad", ["", "not-a-sha", "a" * 63])
def test_a_pin_must_look_like_a_sha256(bad):
    """A truncated or placeholder pin would silently never match, turning the
    guard into a test that always fails — or, if inverted, never fires."""
    assert all(len(s) == 64 and all(c in "0123456789abcdef" for c in s)
               for s in PAGE_PINS.values()), \
        f"every PAGE_PINS value must be a 64-char hex sha256; bad example: {bad!r}"
