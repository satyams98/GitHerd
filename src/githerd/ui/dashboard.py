from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.text import Text

from githerd.bulk import RepoEvent
from githerd.runner import parse_progress
from githerd.ui.rows import RowState, bar, render_row
from githerd.ui.theme import Glyphs


class Dashboard:
    """Pure state plus a `rich` renderable; knows nothing about terminals."""

    def __init__(
        self,
        repos: list[Path],
        branches: dict[str, str],
        glyphs: Glyphs,
        *,
        title: str = "Pulling",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.glyphs = glyphs
        self.title = title
        self._clock = clock
        self.rows: dict[str, RowState] = {
            str(r): RowState(path=str(r), name=r.name, branch=branches.get(str(r), ""))
            for r in repos
        }

    def on_event(self, event: RepoEvent) -> None:
        row = self.rows.get(event.repo)
        if row is None:
            return
        if event.kind == "start":
            row.status = "running"
            row.started = self._clock()
        elif event.kind == "progress":
            parsed = parse_progress(event.text)
            if parsed is not None and event.percent is not None:
                row.phase, row.percent = parsed[0], event.percent
        else:
            row.status = "done"
            row.outcome = event.outcome
            if row.started is not None:
                row.elapsed = self._clock() - row.started

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        total = len(self.rows)
        done = sum(1 for r in self.rows.values() if r.status == "done")
        head = Text()
        head.append(self.title, style="subject")
        head.append(f" {total} repo{'' if total == 1 else 's'}", style="dim")
        right = f"{bar(100 * done // total if total else 100, 12, self.glyphs)} {done}/{total}"
        head.append(" " * max(1, width - len(head.plain) - len(right)))
        head.append(right, style="accent")
        yield head
        name_w = min(24, max((len(r.name) for r in self.rows.values()), default=0))
        branch_w = min(16, max((len(r.branch) for r in self.rows.values()), default=0))
        for row in self.rows.values():
            yield render_row(row, self.glyphs, name_w=name_w, branch_w=branch_w, width=width)


class LiveDashboard:
    """Drives a `rich` Live region from engine events, throttling redraws."""

    def __init__(
        self,
        console: Console,
        dashboard: Dashboard,
        *,
        min_interval: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.console = console
        self.dashboard = dashboard
        self._min_interval = min_interval
        self._clock = clock
        self._last = float("-inf")
        self._live: Live | None = None

    def __enter__(self) -> "LiveDashboard":
        self._live = Live(self.dashboard, console=self.console, auto_refresh=False, transient=False)
        self._live.__enter__()
        return self

    def __exit__(self, *exc_info) -> None:
        assert self._live is not None
        self._live.refresh()
        self._live.__exit__(*exc_info)

    def on_event(self, event: RepoEvent) -> None:
        self.dashboard.on_event(event)
        now = self._clock()
        if event.kind == "done" or now - self._last >= self._min_interval:
            self._last = now
            if self._live is not None:
                self._live.refresh()
