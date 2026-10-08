import io

from githerd.ui.theme import ASCII_GLYPHS, UNICODE_GLYPHS, glyphs_for, make_console


class _Cp1252(io.StringIO):
    encoding = "cp1252"


def test_unicode_glyphs_on_a_utf8_terminal():
    console = make_console(io.StringIO(), force_terminal=True)
    assert glyphs_for(console) is UNICODE_GLYPHS


def test_ascii_glyphs_when_not_a_terminal():
    console = make_console(io.StringIO(), force_terminal=False)
    assert glyphs_for(console) is ASCII_GLYPHS


def test_ascii_glyphs_for_legacy_encoding():
    console = make_console(_Cp1252(), force_terminal=True)
    assert glyphs_for(console) is ASCII_GLYPHS


def test_env_forces_ascii(monkeypatch):
    monkeypatch.setenv("GITHERD_ASCII", "1")
    console = make_console(io.StringIO(), force_terminal=True)
    assert glyphs_for(console) is ASCII_GLYPHS


def test_no_color_is_respected(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    console = make_console(buf, force_terminal=True)
    console.print("[ok]fine[/ok]")
    assert "\x1b[" not in buf.getvalue()


def test_semantic_styles_render_with_colour_on_a_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()
    console = make_console(buf, force_terminal=True)
    console.print("[ok]fine[/ok]")
    assert "\x1b[" in buf.getvalue()


def test_glyph_sets_have_single_cell_status_glyphs():
    for glyphs in (UNICODE_GLYPHS, ASCII_GLYPHS):
        for name in ("ok", "attn", "fail", "running", "queued", "ahead", "behind", "prompt"):
            assert len(getattr(glyphs, name)) == 1
