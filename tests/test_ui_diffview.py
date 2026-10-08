import threading

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from githerd.diffs import FileDiff
from githerd.ui.diffview import ViewerState, run_diff_viewer, sanitize_line, style_line


def run_with_timeout(fn, seconds=10):
    """Run fn in a daemon thread; fail (not hang) if it does not finish in time."""
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"viewer did not exit within {seconds}s (quit key not handled?)")
    if "error" in box:
        raise box["error"]
    return box["result"]


def run_keys(keys, file_list=None):
    with create_pipe_input() as inp:
        inp.send_text(keys)
        return run_with_timeout(
            lambda: run_diff_viewer(
                files() if file_list is None else file_list, input=inp, output=DummyOutput()
            )
        )


def files(n_lines=50):
    body = "\n".join(f"+line{i}" for i in range(n_lines))
    return [FileDiff("a.txt", " M", body), FileDiff("b.txt", "??", "+only")]


@pytest.mark.parametrize("line,style", [
    ("diff --git a/x b/x", "class:diff.header"),
    ("index 1..2 100644", "class:diff.header"),
    ("--- a/x", "class:diff.header"),
    ("+++ b/x", "class:diff.header"),
    ("new file mode 100644", "class:diff.header"),
    ("@@ -1,2 +1,3 @@", "class:diff.hunk"),
    ("+added", "class:diff.add"),
    ("-removed", "class:diff.del"),
    (" context", ""),
    ("", ""),
])
def test_style_line(line, style):
    assert style_line(line) == style


def test_scroll_is_clamped():
    state = ViewerState(files(5))
    state.scroll(-3)
    assert state.offset == 0
    state.scroll(100)
    assert state.offset == 4  # last line index
    state.top()
    assert state.offset == 0
    state.bottom()
    assert state.offset == 4


def test_paging_uses_the_page_size():
    state = ViewerState(files(50), page=10)
    state.page_down()
    assert state.offset == 10
    state.page_up()
    assert state.offset == 0


def test_file_navigation_clamps_and_resets_offset():
    state = ViewerState(files())
    state.scroll(7)
    state.next_file()
    assert (state.index, state.offset) == (1, 0)
    state.next_file()
    assert state.index == 1
    state.prev_file()
    state.prev_file()
    assert state.index == 0


def test_body_is_a_window_of_styled_lines():
    state = ViewerState([FileDiff("a", " M", "@@ x\n+a\n-b\n c")])
    assert state.formatted_body(3) == [
        ("class:diff.hunk", "@@ x\n"), ("class:diff.add", "+a\n"), ("class:diff.del", "-b\n"),
    ]
    state.scroll(3)
    assert state.formatted_body(3) == [("", " c\n")]


def test_header_and_footer():
    state = ViewerState(files())
    assert "a.txt" in state.header() and "1/2" in state.header()
    assert "q quit" in state.footer()


def test_viewer_keys_drive_the_state():
    state = run_keys("jjnq")  # scroll twice, next file, quit
    assert state.index == 1 and state.offset == 0


def test_viewer_scroll_and_back_keys():
    state = run_keys("jjjkq")
    assert state.index == 0 and state.offset == 2


def test_empty_file_list_returns_immediately():
    state = run_with_timeout(lambda: run_diff_viewer([]))
    assert state.files == []


# --- sanitisation of terminal control sequences coming from file contents ---

def test_sanitize_strips_csi_sequences():
    assert sanitize_line("\x1b[31mred\x1b[0m text") == "red text"
    assert sanitize_line("a\x1b[2Jb\x1b[1;1Hc") == "abc"


def test_sanitize_strips_osc_sequences():
    assert sanitize_line("a\x1b]0;evil title\x07b") == "ab"
    assert sanitize_line("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\b") == "alinkb"


def test_sanitize_strips_unterminated_osc_and_lone_escape():
    assert sanitize_line("ok\x1b]0;never terminated") == "ok"
    assert sanitize_line("a\x1bcb") == "ab"  # ESC c (terminal reset) is swallowed
    assert sanitize_line("a\x1bPdcs data\x1b\\b") == "ab"
    assert sanitize_line("end\x1b") == "end"


def test_sanitize_removes_carriage_returns():
    assert sanitize_line("abc\r") == "abc"
    assert sanitize_line("abc\rXYZ") == "abcXYZ"


def test_sanitize_replaces_other_control_characters():
    assert sanitize_line("a\x07b\x00c") == "a?b?c"
    assert sanitize_line("a\x7fb") == "a?b"
    assert sanitize_line("a\x85b\x9bc") == "a?b?c"  # C1 controls


def test_sanitize_expands_tab():
    assert sanitize_line("a\tb") == "a    b"


def test_sanitize_leaves_normal_text_alone():
    for text in ("plain text", "+def f(x): return x", "caf\u00e9 \u00fcber \u65e5\u672c\u8a9e", ""):
        assert sanitize_line(text) == text


def test_body_sanitises_before_styling():
    state = ViewerState([FileDiff("a", " M", "+\x1b[31mred\x1b[0m\r\n-\x07gone")])
    assert state.formatted_body(5) == [
        ("class:diff.add", "+red\n"), ("class:diff.del", "-?gone\n"),
    ]


def test_escape_cannot_hide_a_header_prefix_or_style():
    state = ViewerState([FileDiff("a", " M", "\x1b[0m+++ b/x")])
    assert state.formatted_body(1) == [("class:diff.header", "+++ b/x\n")]


def test_header_is_sanitised_too():
    state = ViewerState([FileDiff("a\x1b[2Jb\x07.txt", " M", "+x")])
    assert state.header().startswith(" ab?.txt")


def test_carriage_return_and_exotic_separators_do_not_split_lines():
    state = ViewerState([FileDiff("a", " M", "+a\rb\x0bc\x0cd\u2028e\n+f\r\n")])
    assert len(state.lines) == 2
    assert state.formatted_body(5) == [
        ("class:diff.add", "+ab?c?d?e\n"), ("class:diff.add", "+f\n"),
    ]


def test_sanitize_replaces_bidi_overrides():
    assert sanitize_line("a\u202eb\u2066c") == "a?b?c"


def test_viewer_top_bottom_and_ctrl_c_quit():
    state = run_keys("Gg\x03")  # bottom, top, Ctrl+C quits
    assert state.index == 0 and state.offset == 0
    state = run_keys("Gq")
    assert state.offset == 49


def test_viewer_arrow_keys_and_paging():
    state = run_keys("\x1b[B\x1b[B\x1b[Aq")  # down, down, up
    assert state.offset == 1
    state = run_keys("bpq")  # page_up / prev_file at the start are no-ops
    assert (state.index, state.offset) == (0, 0)
    state = run_keys(" q")
    assert state.offset == state.page
