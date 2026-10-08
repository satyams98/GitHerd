import io
import sys
import types

import pytest

from githerd.ui.cards import SKIP, Action
from githerd.ui.keys import choose_action, read_key
from githerd.ui.theme import ASCII_GLYPHS, make_console

ACTIONS = [Action("d", "diff", "diff"), Action("s", "stash_pull", "stash & pull"), SKIP]


def keys(*seq):
    it = iter(seq)
    return lambda: next(it)


def console():
    return make_console(io.StringIO(), width=80, force_terminal=False)


def test_matching_is_case_insensitive_and_ignores_other_keys():
    action = choose_action(console(), ACTIONS, ASCII_GLYPHS, keys("x", "", "D"))
    assert action.id == "diff"


def test_escape_means_skip():
    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, keys("\x1b")).id == "skip"


def test_escape_is_ignored_when_skip_is_not_offered():
    only = [Action("r", "retry", "retry")]
    assert choose_action(console(), only, ASCII_GLYPHS, keys("\x1b", "r")).id == "retry"


def test_eof_means_skip():
    def reader():
        raise EOFError

    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, reader).id == "skip"


def test_hint_bar_is_printed_once():
    con = console()
    choose_action(con, ACTIONS, ASCII_GLYPHS, keys("a", "b", "s"))
    out = con.file.getvalue()
    assert out.count("d diff | s stash & pull | k skip") == 1


def test_ctrl_c_propagates():
    def reader():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        choose_action(console(), ACTIONS, ASCII_GLYPHS, reader)


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt is Windows-only")
def test_read_key_handles_plain_special_and_ctrl_c(monkeypatch):
    scripted = iter(["a", "\x00", "H", "\xe0", "K", "\x03"])
    fake = types.SimpleNamespace(getwch=lambda: next(scripted))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)

    assert read_key() == "a"
    assert read_key() == ""  # \x00 prefix swallows the following code
    assert read_key() == ""  # \xe0 prefix swallows the following code
    with pytest.raises(KeyboardInterrupt):
        read_key()
