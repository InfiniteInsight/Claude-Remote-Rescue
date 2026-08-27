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
    62: "7cd573716b8a29fa277dc15b60b25fbbf516c62a14de47658f45e72cffc4f1c9",
    61: "a7cfa17e9c1b981968079d4b5f95e32158ed3d449d6f3177962cd2e1d0516eb4",
    60: "5f759283a79f02ab2d878232eec3d69d063f0e219723f2df77f0f92f7ff178de",
    59: "c8bbc40aae86b0b1b5f4d411dfbca1fd1f8c8313a80c9696d04054fd8d9f0b2f",
    58: "acaef00dba29842887e481c89b369321b5b3d5ea35880cf4aca8ceffe4e1d0dc",
    57: "45b2a2899541aa00cd37d31946957d6d88648e1da6405d4bdd4d0dc10a3a1704",
    56: "42491d0e65ca675f2f34475e4a8ed6d216b231a98e9fd6e337efcea8cd709395",
    55: "48a41cc60dd416d36fd6e3463b42f1ea4eb70d53d80937cd5d07e208f21f4367",
    54: "13c341f7136823a6d19498f29de77ee62f6a8495d56f13de5adbe716df7af0dc",
    53: "53b0258901cbd25ef8ae2071a7d47309514479ccf6adb24d21bdaad617422a81",
    52: "71c6e1929ee03d8f7a949ee390c32254ee5518dbb107a20b5af5fd215faa5d13",
    51: "bb44312c2831009735bc7447b450a395e4b708d86114b406805f345e8c6f0313",
    50: "c5767b42064bffce34d3f54232e6d2f7ec086e5dcfc2154ed3964c5592d82b18",
    49: "1751a63f6ad7781bca09c22c3d775c8a7dbdd6d25e64f5d647b881f4a612846e",
    45: "f18d2516203d2868d60452cf478bc19dd3b36f802f9df066c77b40e17bf2d7e9",
    46: "392b299ae9394da5dea45691909e6b5f442ce3f400ab51ead930d2ddacc08d18",
    47: "fc8299db259d6a2c28f2f73720bf451800704cee86a7a9a637cd79cd0e5678e2",
    48: "8e682af02b000222206c9d373070a67175065d4ab69d6fea44dcf7acdf01327e",
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


# Versions bumped ahead of their own page.html change (see the PAGE_PINS
# comment above) — the ONLY sanctioned way for two entries to share a hash.
# Remove an entry once its page.html change lands and its pin is updated.
_PENDING_PAGE_CHANGE: set[int] = set()


def test_no_two_versions_share_a_page_hash():
    """Catches a copy-pasted entry — a new version pinned to the old page,
    which would let the next real page change slip through unnoticed."""
    seen: dict[str, int] = {}
    for version, sha in PAGE_PINS.items():
        if version in _PENDING_PAGE_CHANGE:
            continue
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
