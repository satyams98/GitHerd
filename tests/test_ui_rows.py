from pathlib import Path

import pytest
from rich.cells import cell_len

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Diverged, Failed, FileChange, NetworkError, Ok, UpToDate,
)
from githerd.repos import RepoSnapshot
from githerd.ui.rows import RowState, bar, fit, render_row, render_status
from githerd.ui.theme import ASCII_GLYPHS, UNICODE_GLYPHS


def row(**kw) -> RowState:
    return RowState(path="/w/api", name="api", branch="main", **kw)


def test_bar_fills_proportionally():
    assert bar(50, 10, ASCII_GLYPHS) == "#####-----"
    assert bar(0, 4, ASCII_GLYPHS) == "----"
    assert bar(100, 4, ASCII_GLYPHS) == "####"
    assert bar(250, 4, ASCII_GLYPHS) == "####"  # clamped


def test_fit_truncates_with_ellipsis():
    assert fit("short", 10, UNICODE_GLYPHS) == "short"
    assert fit("a-very-long-name", 8, UNICODE_GLYPHS) == "a-very-…"


def test_fit_width_zero_and_negative_is_empty():
    assert fit("abc", 0, ASCII_GLYPHS) == ""
    assert fit("abc", -3, UNICODE_GLYPHS) == ""


def test_fit_width_one():
    assert cell_len(fit("abcdef", 1, UNICODE_GLYPHS)) <= 1
    assert cell_len(fit("abcdef", 1, ASCII_GLYPHS)) <= 1
    assert fit("a", 1, ASCII_GLYPHS) == "a"


def test_fit_exact_fit_unchanged():
    assert fit("abcdef", 6, ASCII_GLYPHS) == "abcdef"
    assert fit("abcdef", 6, UNICODE_GLYPHS) == "abcdef"


def test_fit_ascii_marker():
    assert fit("a-very-long-name", 8, ASCII_GLYPHS) == "a-ver..."


def test_fit_unicode_marker():
    assert fit("a-very-long-name", 8, UNICODE_GLYPHS) == "a-very-…"


def test_fit_wide_characters():
    out = fit("日本語リポ", 7, UNICODE_GLYPHS)
    assert cell_len(out) <= 7
    assert out.endswith("…")
    out = fit("日本語リポ", 7, ASCII_GLYPHS)
    assert cell_len(out) <= 7
    assert out.endswith("...")


def _done_row(**kw) -> RowState:
    return row(status="done", outcome=Failed(message="x" * 200), elapsed=2.0, **kw)


@pytest.mark.parametrize("glyphs", [ASCII_GLYPHS, UNICODE_GLYPHS])
@pytest.mark.parametrize("width", [0, 1, 3, 5, 6, 10])
def test_done_row_with_elapsed_is_exactly_width_cells(glyphs, width):
    text = render_row(_done_row(), glyphs, name_w=3, branch_w=4, width=width)
    assert cell_len(text.plain) == width


@pytest.mark.parametrize("width", [0, 1, 3, 5, 6, 10])
def test_running_and_queued_rows_are_exactly_width_cells(width):
    running = row(status="running", phase="Receiving objects", percent=78)
    queued = row()
    for r in (running, queued):
        text = render_row(r, ASCII_GLYPHS, name_w=3, branch_w=4, width=width)
        assert cell_len(text.plain) == width


def test_wide_name_row_is_exactly_width_cells():
    r = RowState(path="/w/x", name="日本語リポ", branch="main", status="done",
                 outcome=Failed(message="x" * 200), elapsed=2.0)
    text = render_row(r, UNICODE_GLYPHS, name_w=7, branch_w=4, width=40)
    assert cell_len(text.plain) == 40
    assert text.plain.endswith("2.0s")


def test_queued_row():
    plain = render_row(row(), UNICODE_GLYPHS, name_w=6, branch_w=6, width=60).plain
    assert plain.startswith("· api   ")
    assert "queued" in plain


def test_running_row_shows_phase_bar_and_percent():
    r = row(status="running", phase="Receiving objects", percent=78)
    plain = render_row(r, ASCII_GLYPHS, name_w=3, branch_w=4, width=70).plain
    assert plain.startswith("* api")
    assert "Receiving objects" in plain and "78%" in plain and "#" in plain


def test_running_row_without_percent_says_working():
    plain = render_row(row(status="running"), ASCII_GLYPHS, name_w=3, branch_w=4, width=60).plain
    assert "working" in plain


def test_done_ok_row_has_summary_and_elapsed_at_the_right_edge():
    outcome = Ok(commits=3, files=12, before_head="a", after_head="b")
    r = row(status="done", outcome=outcome, elapsed=1.234)
    text = render_row(r, UNICODE_GLYPHS, name_w=3, branch_w=4, width=60)
    assert text.plain.startswith("✓ api")
    assert "updated: 3 commits, 12 files" in text.plain
    assert text.plain.endswith("1.2s")
    assert len(text.plain) == 60


def test_up_to_date_row_is_dim():
    text = render_row(row(status="done", outcome=UpToDate()), UNICODE_GLYPHS, name_w=3, branch_w=4, width=60)
    assert "already up to date" in text.plain
    assert any("dim" in str(span.style) for span in text.spans)


def test_attention_and_failure_glyphs():
    blocked = row(status="done", outcome=BlockedDirty(files=[FileChange(status=" M", path="a")]))
    auth = row(status="done", outcome=AuthRequired(remote="origin"))
    failed = row(status="done", outcome=Failed(message="boom"))
    assert render_row(blocked, ASCII_GLYPHS, name_w=3, branch_w=4, width=60).plain.startswith("! ")
    assert render_row(auth, ASCII_GLYPHS, name_w=3, branch_w=4, width=60).plain.startswith("! ")
    assert render_row(failed, ASCII_GLYPHS, name_w=3, branch_w=4, width=60).plain.startswith("x ")


def test_row_is_truncated_never_wrapped():
    r = row(status="done", outcome=Failed(message="x" * 200), elapsed=2.0)
    text = render_row(r, ASCII_GLYPHS, name_w=3, branch_w=4, width=40)
    assert len(text.plain) == 40
    assert "..." in text.plain
    assert text.plain.endswith("2.0s")


def test_row_is_truncated_never_wrapped_unicode():
    r = row(status="done", outcome=Failed(message="x" * 200), elapsed=2.0)
    text = render_row(r, UNICODE_GLYPHS, name_w=3, branch_w=4, width=40)
    assert len(text.plain) == 40
    assert "…" in text.plain
    assert text.plain.endswith("2.0s")


def test_ascii_glyphs_emit_only_ascii_for_truncated_rows():
    long_name = RowState(path="/w/x", name="a-very-long-repository-name", branch="feature/very-long-branch",
                         status="done", outcome=Failed(message="x" * 200), elapsed=2.0)
    for width in (0, 1, 5, 20, 40):
        assert render_row(long_name, ASCII_GLYPHS, name_w=8, branch_w=6, width=width).plain.isascii()
    running = RowState(path="/w/x", name="a-very-long-repository-name", branch="b", status="running",
                       phase="A very long phase name here", percent=40)
    assert render_row(running, ASCII_GLYPHS, name_w=8, branch_w=6, width=30).plain.isascii()


def test_ascii_glyphs_render_status_long_name_is_ascii():
    snaps = [
        _snap("a-very-long-repository-name-over-limit", branch="feature/a-very-long-branch-name",
              upstream="origin/x", ahead=1, behind=2),
        _snap("short", error="fatal: boom"),
    ]
    assert render_status(snaps, ASCII_GLYPHS).plain.isascii()


def test_diverged_conflict_network_error_glyphs():
    diverged = row(status="done", outcome=Diverged(ahead=1, behind=2))
    conflict = row(status="done", outcome=Conflict(files=["a"]))
    network = row(status="done", outcome=NetworkError(message="down"))
    assert render_row(diverged, ASCII_GLYPHS, name_w=3, branch_w=4, width=60).plain.startswith("! ")
    assert render_row(conflict, ASCII_GLYPHS, name_w=3, branch_w=4, width=60).plain.startswith("! ")
    assert render_row(network, ASCII_GLYPHS, name_w=3, branch_w=4, width=60).plain.startswith("x ")


def test_done_without_outcome_shows_no_result_as_attention():
    plain = render_row(row(status="done"), ASCII_GLYPHS, name_w=3, branch_w=4, width=60).plain
    assert plain.startswith("! ")
    assert "no result" in plain


def test_bar_truncates_never_over_reports():
    assert bar(99, 10, ASCII_GLYPHS) == "#########-"
    assert bar(95, 10, ASCII_GLYPHS) == "#########-"


def _snap(name, **kw) -> RepoSnapshot:
    return RepoSnapshot(path=Path(name), name=name, **kw)


def test_render_status_lines():
    snaps = [
        _snap("alpha", branch="main", upstream="origin/main", ahead=1, behind=2),
        _snap("beta", branch="dev", upstream="origin/dev",
              dirty=[FileChange(status=" M", path="a.txt")]),
        _snap("gamma", branch=None),
        _snap("delta", error="fatal: not a git repository\nsecond line"),
    ]
    lines = render_status(snaps, ASCII_GLYPHS).plain.splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("+ alpha") and "^1 v2" in lines[0] and "clean" in lines[0]
    assert lines[1].startswith("! beta") and "1 changed" in lines[1]
    assert "(detached)" in lines[2] and "no upstream" in lines[2]
    assert lines[3].startswith("x delta") and "error: fatal: not a git repository" in lines[3]
    assert "second line" not in lines[3]


def test_render_status_empty_is_empty():
    assert render_status([], ASCII_GLYPHS).plain == ""


def test_render_status_wide_name_keeps_columns_aligned():
    snaps = [
        _snap("日本語リポ", branch="main", upstream="origin/main"),
        _snap("alpha", branch="main", upstream="origin/main"),
    ]
    lines = render_status(snaps, UNICODE_GLYPHS).plain.splitlines()
    assert len(lines) == 2
    prefix0 = lines[0][: lines[0].index("main")]
    prefix1 = lines[1][: lines[1].index("main")]
    assert cell_len(prefix0) == cell_len(prefix1)
