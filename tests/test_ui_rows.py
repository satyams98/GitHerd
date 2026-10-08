from pathlib import Path

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Failed, FileChange, Ok, UpToDate,
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
    assert fit("short", 10) == "short"
    assert fit("a-very-long-name", 8) == "a-very-…"


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
    assert "…" in text.plain
    assert text.plain.endswith("2.0s")


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
