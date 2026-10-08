import io
import sys
import types

import pytest

from githerd.ui.cards import SKIP, Action
from githerd.ui.keys import choose_action, flush_input, read_key
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
    assert read_key() == "\x00"  # \x00 prefix swallows the following code; "" means only "nothing came"
    assert read_key() == "\x00"  # \xe0 prefix swallows the following code
    with pytest.raises(KeyboardInterrupt):
        read_key()


def counting_reader(fn, limit=200):
    """Reader that fails the test (instead of hanging) if polled too often."""
    calls = {"n": 0}

    def reader():
        calls["n"] += 1
        if calls["n"] > limit:
            pytest.fail(f"reader polled more than {limit} times (spin)")
        return fn(calls["n"])

    reader.calls = calls
    return reader


def test_endless_empty_keys_return_skip_within_limit():
    reader = counting_reader(lambda n: "")
    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, reader).id == "skip"
    assert reader.calls["n"] <= 100


def test_endless_empty_keys_without_skip_raise_eof():
    only = [Action("r", "retry", "retry")]
    reader = counting_reader(lambda n: "")
    with pytest.raises(EOFError):
        choose_action(console(), only, ASCII_GLYPHS, reader)
    assert reader.calls["n"] <= 100


def test_real_key_resets_empty_counter():
    # 49 empties, a non-matching key, 49 empties, then a match: never dead.
    seq = [""] * 49 + ["x"] + [""] * 49 + ["d"]
    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, keys(*seq)).id == "diff"


def test_oserror_means_skip():
    def reader():
        raise OSError

    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, reader).id == "skip"


@pytest.mark.parametrize("exc", [EOFError, OSError])
def test_reader_failure_without_skip_propagates(exc):
    only = [Action("r", "retry", "retry")]

    def reader():
        raise exc

    with pytest.raises(exc):
        choose_action(console(), only, ASCII_GLYPHS, reader)


def test_empty_actions_raises_value_error_immediately():
    reader = counting_reader(lambda n: "x", limit=0)
    with pytest.raises(ValueError, match="no actions to choose from"):
        choose_action(console(), [], ASCII_GLYPHS, reader)


def test_duplicate_keys_raise_value_error():
    dup = [Action("d", "diff", "diff"), Action("D", "drop", "drop")]
    with pytest.raises(ValueError, match="d"):
        choose_action(console(), dup, ASCII_GLYPHS, keys("d"))


def test_uppercase_action_key_matches_lowercase_input():
    acts = [Action("D", "diff", "diff"), SKIP]
    assert choose_action(console(), acts, ASCII_GLYPHS, keys("d")).id == "diff"


def test_enter_is_ignored_and_loop_continues():
    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, keys("\r", "\n", "d")).id == "diff"


def test_read_key_non_windows_eof_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(EOFError):
        read_key()


def test_read_key_non_windows_returns_char(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", io.StringIO("q"))
    assert read_key() == "q"


# ---- H4 items 4 and 5: typed-ahead flush and special keys -----------------------------------

def test_flush_runs_once_after_the_hint_bar_and_before_the_first_read():
    con = console()
    events = []

    def flush():
        events.append(("flush", con.file.getvalue()))

    def reader():
        events.append(("read", ""))
        return "d"

    choose_action(con, ACTIONS, ASCII_GLYPHS, reader, flush)
    assert [name for name, _ in events] == ["flush", "read"]
    assert "d diff | s stash & pull | k skip" in events[0][1]  # the hint bar was already printed


def test_flush_is_called_exactly_once_however_many_keys_are_read():
    calls = []
    choose_action(console(), ACTIONS, ASCII_GLYPHS, keys("x", "y", "z", "d"), lambda: calls.append(1))
    assert calls == [1]


def test_an_exception_from_flush_is_swallowed():
    def flush():
        raise RuntimeError("console exploded")

    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, keys("d"), flush).id == "diff"


def _fake_msvcrt(monkeypatch, pending):
    consumed = []
    fake = types.SimpleNamespace(
        kbhit=lambda: bool(pending),
        getwch=lambda: consumed.append(pending.pop(0)) or consumed[-1],
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    return consumed


def test_flush_input_drains_every_pending_key_on_windows(monkeypatch):
    pending = ["a", "b", "\x00", "H", "c"]
    consumed = _fake_msvcrt(monkeypatch, pending)
    flush_input()
    assert consumed == ["a", "b", "\x00", "H", "c"] and pending == []


def test_flush_input_with_nothing_pending_reads_nothing(monkeypatch):
    consumed = _fake_msvcrt(monkeypatch, [])
    flush_input()
    assert consumed == []


def test_flush_input_tolerates_an_oserror(monkeypatch):
    def boom():
        raise OSError("no console")

    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(kbhit=boom, getwch=boom))
    monkeypatch.setattr(sys, "platform", "win32")
    flush_input()  # must not raise


def test_flush_input_is_a_no_op_off_windows(monkeypatch):
    def boom():
        raise AssertionError("msvcrt must not be touched off Windows")

    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(kbhit=boom, getwch=boom))
    monkeypatch.setattr(sys, "platform", "linux")
    assert flush_input() is None


def test_special_key_sentinel_is_never_an_action_and_never_trips_the_dead_reader_rule():
    seq = ["\x00"] * 200 + ["d"]  # a held arrow key, then a real choice
    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, keys(*seq)).id == "diff"


def test_special_keys_reset_the_empty_counter():
    seq = [""] * 49 + ["\x00"] + [""] * 49 + ["d"]
    assert choose_action(console(), ACTIONS, ASCII_GLYPHS, keys(*seq)).id == "diff"


def test_the_sentinel_does_not_select_an_action_even_if_one_is_bound_to_it():
    odd = [Action("\x00", "weird", "weird"), SKIP]
    assert choose_action(console(), odd, ASCII_GLYPHS, keys("\x00", "k")).id == "skip"
