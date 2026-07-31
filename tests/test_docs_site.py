"""Static docs site: self-contained, dependency-free, honest about status.

Content requirements come from the Task 4 brief
(.superpowers/sdd/2026-07-31-restore-prompt-and-docs-site/task-4-brief.md):
zero external requests, the "Not affiliated with Anthropic" disclaimer, and
the current CLI command surface (extended here with `crr untmux`, which the
brief's own snippet omitted despite listing `crr rescued`).
"""

from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "docs" / "site"


class _Checker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.external = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if (
                k in ("src", "href")
                and v
                and v.startswith(("http://", "https://"))
                and "github.com" not in v
            ):
                self.external.append(v)


def test_site_exists_parses_and_is_self_contained():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    checker = _Checker()
    checker.feed(html)
    assert checker.external == []  # no CDN/webfonts/analytics
    assert "Not affiliated with Anthropic" in html
    assert (SITE / "style.css").is_file()
    assert (SITE / "dashboard.png").is_file()


def test_site_has_no_script_tags():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    assert "<script" not in html.lower()


def test_site_commands_match_cli_surface():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for cmd in (
        "crr status",
        "crr reopen",
        "crr kick",
        "crr close",
        "crr dismiss",
        "crr detmux",
        "crr untmux",
        "crr rescued",
        "crr doctor",
        "crr systemd",
    ):
        assert cmd in html, f"missing command: {cmd}"


def test_site_has_calibration_footer():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    assert "Linux/WSL is live-verified" in html
    assert "macOS/Windows adapters are unit-tested" in html
    assert "hardware verification pending" in html


def test_site_links_stylesheet_and_screenshot_locally():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    assert 'href="style.css"' in html
    assert 'src="dashboard.png"' in html


def test_style_css_is_dark_scheme_aware():
    css = (SITE / "style.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme" in css
