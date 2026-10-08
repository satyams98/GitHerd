import io
from pathlib import Path

from githerd.bulk import RepoEvent
from githerd.outcomes import BlockedDirty, FileChange, Ok, UpToDate
from githerd.ui.dashboard import Dashboard, LiveDashboard
from githerd.ui.theme import ASCII_GLYPHS, make_console
from tests.helpers_ui import render_plain


def make_dashboard(clock=None):
    repos = [Path("/w/api"), Path("/w/billing"), Path("/w/docs")]
    branches = {"/w/api": "main", "/w/billing": "dev"}
    kwargs = {"clock": clock} if clock else {}
    return Dashboard(repos, branches, ASCII_GLYPHS, title="Pulling", **kwargs), repos


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
