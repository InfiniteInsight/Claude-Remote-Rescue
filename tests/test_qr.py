from crr.core import qr


def test_to_terminal_is_nonempty_block_text():
    out = qr.to_terminal("https://lovelace.tail3af2d9.ts.net/")
    assert isinstance(out, str) and out.strip()
    # QR terminal art is drawn with block glyphs / spaces; assert it's a grid.
    assert any(ch in out for ch in ("█", "▀", "▄", "█", " "))
    assert "\n" in out  # multiple rows


def test_to_svg_is_an_svg_document():
    out = qr.to_svg("https://lovelace.tail3af2d9.ts.net/")
    assert isinstance(out, str)
    assert "<svg" in out and "</svg>" in out


def test_encoding_is_deterministic():
    url = "https://lovelace.tail3af2d9.ts.net/"
    assert qr.to_terminal(url) == qr.to_terminal(url)
    assert qr.to_svg(url) == qr.to_svg(url)


def test_distinct_inputs_differ():
    assert qr.to_svg("https://a.ts.net/") != qr.to_svg("https://b.ts.net/")
