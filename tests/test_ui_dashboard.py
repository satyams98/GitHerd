import io
from pathlib import Path

import pytest
from rich.cells import cell_len

from githerd.bulk import RepoEvent
from githerd.outcomes import BlockedDirty, FileChange, Ok, UpToDate
from githerd.ui.dashboard import Dashboard, LiveDashboard
from githerd.ui.theme import ASCII_GLYPHS, UNICODE_GLYPHS, make_console
from tests.helpers_ui import render_plain


def make_dashboard(clock=None, glyphs=ASCII_GLYPHS, title="Pulling"):
    repos = [Path("/w/api"), Path("/w/billing"), Path("/w/docs")]
    branches = {"/w/api": "main", "/w/billing": "dev"}
    kwargs = {"clock": clock} if clock else {}
    return Dashboard(repos, branches, glyphs, title=title, **kwargs), repos


def ev(repo, kind, **kw):
    return RepoEvent(repo=str(Path("/w") / repo), kind=kind, **kw)


def test_initial_frame_lists_every_repo_as_queued():
    dash, _ = make_dashboard()
    text = render_plain(dash)
    assert "Pulling 3 repos" in text and "0/3" in text
    assert text.count("queued") == 3
    assert [r.name for r in dash.rows.values()] == ["api", "billing", "docs"]


def test_events_drive_row_states_and_header_counts():
    ticks = iter([10.0, 10.0, 11.5, 12.0])
    dash, _ = make_dashboard(clock=lambda: next(ticks))
    dash.on_event(ev("api", "start"))
    dash.on_event(ev("billing", "start"))
    dash.on_event(ev("billing", "progress", text="Receiving objects:  78% (78/100)", percent=78))
    dash.on_event(ev("api", "done", outcome=Ok(commits=2, files=3, before_head="a", after_head="b")))
    text = render_plain(dash)
    assert "1/3" in text
    assert "updated: 2 commits, 3 files" in text and "1.5s" in text
    assert "Receiving objects" in text and "78%" in text
    assert text.count("queued") == 1


def test_non_progress_noise_is_ignored():
    dash, _ = make_dashboard()
    dash.on_event(ev("api", "start"))
    dash.on_event(ev("api", "progress", text="From /somewhere", percent=None))
    assert dash.rows[str(Path("/w/api"))].percent is None


def test_unknown_repo_event_is_ignored():
    dash, _ = make_dashboard()
    dash.on_event(RepoEvent(repo="/elsewhere", kind="done", outcome=UpToDate()))
    assert "0/3" in render_plain(dash)


def test_attention_row_is_visible_in_the_frame():
    dash, _ = make_dashboard()
    dash.on_event(ev("docs", "start"))
    dash.on_event(ev("docs", "done", outcome=BlockedDirty(files=[FileChange(status=" M", path="a")])))
    assert "blocked: 1 local change" in render_plain(dash)


def test_live_dashboard_leaves_final_frame_in_scrollback():
    console = make_console(io.StringIO(), width=80, force_terminal=True, record=True)
    dash, _ = make_dashboard()
    with LiveDashboard(console, dash) as live:
        for name in ("api", "billing", "docs"):
            live.on_event(ev(name, "start"))
            live.on_event(ev(name, "done", outcome=UpToDate()))
        assert live._live.transient is False
    text = console.export_text()
    assert "3/3" in text
    assert text.count("already up to date") >= 3


def test_live_dashboard_throttles_refreshes():
    now = [0.0]
    console = make_console(io.StringIO(), width=80, force_terminal=True)
    dash, _ = make_dashboard()
    with LiveDashboard(console, dash, min_interval=0.05, clock=lambda: now[0]) as live:
        calls = []
        live._live.refresh = lambda: calls.append(now[0])
        for t in (0.00, 0.01, 0.02, 0.06):  # progress events
            now[0] = t
            live.on_event(ev("api", "progress", text="Receiving objects:  10% (1/10)", percent=10))
        assert calls == [0.00, 0.06]
        now[0] = 0.061
        live.on_event(ev("api", "done", outcome=UpToDate()))  # done always refreshes
        assert calls[-1] == 0.061


@pytest.mark.parametrize("glyphs", [UNICODE_GLYPHS, ASCII_GLYPHS], ids=["unicode", "ascii"])
@pytest.mark.parametrize("width", [*range(0, 41), 80])
def test_every_line_fits_the_width_and_header_is_one_line(width, glyphs):
    dash, _ = make_dashboard(glyphs=glyphs)
    text = render_plain(dash, width)
    if width == 0:
        assert text == ""  # rich emits nothing at zero width: no wrapped debris either
        return
    lines = text.split("\n")[:-1]
    assert len(lines) == 1 + 3  # header + one line per repo: stable frame height
    assert all(cell_len(line) <= width for line in lines)
    if glyphs is ASCII_GLYPHS:
        assert text.isascii()


@pytest.mark.parametrize("width", [0, 1, 3, 10, 17, 18, 30, 80])
def test_wide_character_title_keeps_header_on_one_line(width):
    dash, _ = make_dashboard(title="拉取全部仓库")
    text = render_plain(dash, width)
    if width == 0:
        assert text == ""
        return
    lines = text.split("\n")[:-1]
    assert len(lines) == 4
    assert all(cell_len(line) <= width for line in lines)
    if width == 80:
        assert "拉取全部仓库" in lines[0] and "0/3" in lines[0]
        assert cell_len(lines[0]) == 80


def test_header_is_padded_to_exact_width_when_it_fits():
    dash, _ = make_dashboard()
    header = render_plain(dash, 60).split("\n")[0]
    assert cell_len(header) == 60 and header.startswith("Pulling 3 repos") and header.endswith("0/3")


def test_narrow_header_shows_only_the_counter():
    dash, _ = make_dashboard()
    assert render_plain(dash, 5).split("\n")[0] == "0/3"


def test_zero_repos_renders_without_error():
    dash = Dashboard([], {}, ASCII_GLYPHS, title="Pulling")
    text = render_plain(dash)
    assert "0/0" in text and "0 repos" in text
    assert len(text.split("\n")[:-1]) == 1


def test_duplicate_and_late_events_do_not_regress_a_done_row():
    ticks = iter([1.0, 3.0, 99.0, 99.0, 99.0])
    dash, _ = make_dashboard(clock=lambda: next(ticks))
    row = dash.rows[str(Path("/w/api"))]
    dash.on_event(ev("api", "start"))
    dash.on_event(ev("api", "done", outcome=UpToDate()))
    assert (row.status, row.elapsed) == ("done", 2.0)
    dash.on_event(ev("api", "start"))
    dash.on_event(ev("api", "progress", text="Receiving objects:  10% (1/10)", percent=10))
    dash.on_event(ev("api", "done", outcome=Ok(commits=1, files=1, before_head="a", after_head="b")))
    assert row.status == "done" and row.elapsed == 2.0
    assert isinstance(row.outcome, UpToDate)
    assert row.percent is None and row.phase == ""


def test_start_clears_stale_progress():
    dash, _ = make_dashboard()
    row = dash.rows[str(Path("/w/api"))]
    dash.on_event(ev("api", "start"))
    dash.on_event(ev("api", "progress", text="Receiving objects:  10% (1/10)", percent=10))
    assert row.percent == 10 and row.phase
    dash.on_event(ev("api", "start"))
    assert row.percent is None and row.phase == "" and row.status == "running"


def test_unknown_event_kind_is_ignored_not_treated_as_done():
    dash, _ = make_dashboard()
    dash.on_event(ev("api", "start"))
    dash.on_event(RepoEvent.model_construct(repo=str(Path("/w/api")), kind="bogus", text="", percent=None, outcome=None))
    row = dash.rows[str(Path("/w/api"))]
    assert row.status == "running" and row.outcome is None


def test_exception_inside_with_propagates_and_stops_live():
    console = make_console(io.StringIO(), width=80, force_terminal=True)
    dash, _ = make_dashboard()
    captured = []
    with pytest.raises(ValueError, match="boom"):
        with LiveDashboard(console, dash) as live:
            captured.append(live._live)
            assert captured[0].is_started
            raise ValueError("boom")
    assert captured[0].is_started is False
    with LiveDashboard(console, dash) as again:  # console is reusable afterwards
        again.on_event(ev("api", "start"))


class FailingFile(io.StringIO):
    """File-like that raises OSError on write once `fail` is switched on."""

    fail = False

    def write(self, s):
        if self.fail:
            raise OSError("broken pipe")
        return super().write(s)


def test_teardown_write_failure_does_not_mask_the_original_exception():
    file = FailingFile()
    console = make_console(file, width=80, force_terminal=True)
    dash, _ = make_dashboard()
    captured = []
    with pytest.raises(ValueError, match="boom"):
        with LiveDashboard(console, dash) as live:
            captured.append(live._live)
            file.fail = True
            raise ValueError("boom")
    assert captured[0].is_started is False


def test_teardown_write_failure_without_other_error_still_stops_live():
    file = FailingFile()
    console = make_console(file, width=80, force_terminal=True)
    dash, _ = make_dashboard()
    captured = []
    with pytest.raises(OSError):
        with LiveDashboard(console, dash) as live:
            captured.append(live._live)
            file.fail = True
    assert captured[0].is_started is False


def test_late_events_after_exit_update_state_but_do_not_refresh():
    console = make_console(io.StringIO(), width=80, force_terminal=True)
    dash, _ = make_dashboard()
    with LiveDashboard(console, dash) as live:
        inner = live._live
        calls = []
        inner.refresh = lambda: calls.append(1)
    calls.clear()  # Live.stop() does its own final refresh
    live.on_event(ev("api", "done", outcome=UpToDate()))
    assert calls == []
    assert dash.rows[str(Path("/w/api"))].status == "done"


def test_live_dashboard_final_frame_survives_without_explicit_refresh():
    console = make_console(io.StringIO(), width=80, force_terminal=True, record=True)
    dash, _ = make_dashboard()
    with LiveDashboard(console, dash, min_interval=3600.0, clock=lambda: 0.0) as live:
        live.on_event(ev("api", "start"))
        live.on_event(ev("api", "progress", text="Receiving objects:  10% (1/10)", percent=10))
    text = console.export_text()  # clears the record buffer, so read it once
    assert "Receiving objects" in text  # only the final frame has it: stop() redraws


def test_header_is_empty_at_zero_width():
    dash, _ = make_dashboard()
    assert dash._header(0).plain == ""


def test_dashboard_row_for_a_hostile_repo_name_has_no_control_characters_and_aligned_columns():
    evil = "evil\u202e\x1b[2Jname"
    dash = Dashboard([Path("/w") / evil, Path("/w/b")], {}, ASCII_GLYPHS)
    text = render_plain(dash, width=60)
    assert "\x1b" not in text and "\u202e" not in text
    lines = [ln for ln in text.splitlines() if "queued" in ln]
    assert len(lines) == 2
    assert lines[0].index("queued") == lines[1].index("queued")  # widths use the cleaned names
    assert lines[0].split()[1] == "evil?name"  # safe_path shows the override as ?


# ---- H5 item 1: the real git command, shown quietly ------------------------------------------

COMMAND = "git pull --ff-only --progress"


def make_dashboard_with_command(glyphs=ASCII_GLYPHS):
    repos = [Path("/w/api"), Path("/w/billing"), Path("/w/docs")]
    return Dashboard(repos, {}, glyphs, command=COMMAND)


def test_the_pull_command_constant_is_the_command_that_runs():
    from githerd import gitops

    assert gitops.PULL_COMMAND == COMMAND
    assert gitops.PULL_COMMAND.split()[:2] == ["git", "pull"]


def test_a_dashboard_without_a_command_has_no_command_line():
    dash, _ = make_dashboard()
    text = render_plain(dash)
    assert "$ git" not in text
    assert len(text.split("\n")[:-1]) == 4


def test_the_command_is_one_extra_last_line_right_aligned_within_the_width():
    lines = render_plain(make_dashboard_with_command(), 80).split("\n")[:-1]
    assert len(lines) == 1 + 3 + 1
    shown = "$ " + COMMAND
    assert lines[-1] == " " * (80 - len(shown)) + shown


def test_the_command_line_is_dim():
    console = make_console(io.StringIO(), width=80)
    parts = list(make_dashboard_with_command().__rich_console__(console, console.options))
    assert parts[-1].plain.strip() == "$ " + COMMAND
    assert parts[-1].style == "dim"


@pytest.mark.parametrize("glyphs", [UNICODE_GLYPHS, ASCII_GLYPHS], ids=["unicode", "ascii"])
@pytest.mark.parametrize("width", [*range(0, 41), 80])
def test_the_command_line_never_wraps_at_any_width(width, glyphs):
    text = render_plain(make_dashboard_with_command(glyphs), width)
    if width == 0:
        assert text == ""
        return
    lines = text.split("\n")[:-1]
    assert len(lines) == 1 + 3 + 1  # stable frame height: header, rows, command
    assert all(cell_len(line) <= width for line in lines)
    if glyphs is ASCII_GLYPHS:
        assert text.isascii()


def test_the_command_survives_in_the_final_live_frame():
    console = make_console(io.StringIO(), width=80, force_terminal=True, record=True)
    dash = make_dashboard_with_command()
    with LiveDashboard(console, dash) as live:
        live.on_event(ev("api", "start"))
        live.on_event(ev("api", "done", outcome=UpToDate()))
    assert "$ " + COMMAND in console.export_text()


# ---- H6 item 6: labels for duplicate repo names ------------------------------------------------

def test_labels_replace_the_directory_name_in_the_rows():
    repos = [Path("/w/a/api"), Path("/w/b/api"), Path("/w/docs")]
    dash = Dashboard(repos, {}, ASCII_GLYPHS, labels={repos[0]: "a/api", repos[1]: "b/api"})
    text = render_plain(dash, 80)
    assert "a/api" in text and "b/api" in text and "docs" in text
    assert [r.name for r in dash.rows.values()] == ["a/api", "b/api", "docs"]


def test_without_labels_rows_show_the_directory_name():
    repos = [Path("/w/a/api"), Path("/w/docs")]
    dash = Dashboard(repos, {}, ASCII_GLYPHS)
    assert [r.name for r in dash.rows.values()] == ["api", "docs"]
