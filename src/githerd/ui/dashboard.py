from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.text import Text

from githerd.bulk import RepoEvent
from githerd.runner import parse_progress
from githerd.textsafe import safe_path
from githerd.ui.rows import RowState, bar, fit, render_row
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
        if row is None or row.status == "done":
            return  # unknown repo, or a late/duplicate event for a finished row
        if event.kind == "start":
            row.status = "running"
            row.started = self._clock()
            row.phase, row.percent = "", None
        elif event.kind == "progress":
            parsed = parse_progress(event.text)
            if parsed is not None and event.percent is not None:
                row.phase, row.percent = parsed[0], event.percent
        elif event.kind == "done":
            row.status = "done"
            row.outcome = event.outcome
            if row.started is not None:
                row.elapsed = self._clock() - row.started
        # any other kind is ignored

    def _header(self, width: int) -> Text:
        total = len(self.rows)
        done = sum(1 for r in self.rows.values() if r.status == "done")
        counter = f"{done}/{total}"
        right = f"{bar(100 * done // total if total else 100, 12, self.glyphs)} {counter}"
        right_w = cell_len(right)
        if width < right_w + 1:
            return Text(fit(counter, width, self.glyphs), style="accent")
        count = f" {total} repo{'' if total == 1 else 's'}"
        left = fit(self.title + count, width - right_w - 1, self.glyphs)
        head = Text()
        head.append(left[: len(self.title)], style="subject")
        head.append(left[len(self.title):], style="dim")
        head.append(" " * (width - cell_len(left) - right_w))
        head.append(right, style="accent")
        return head

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(options.max_width, 0)
        yield self._header(width)
        name_w = min(24, max((cell_len(safe_path(r.name)) for r in self.rows.values()), default=0))
        branch_w = min(16, max((cell_len(r.branch) for r in self.rows.values()), default=0))
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
        live = Live(self.dashboard, console=self.console, auto_refresh=False, transient=False)
        try:
            live.__enter__()
        except BaseException:
            try:
                live.stop()
            except Exception:
                pass
            raise
        self._live = live
        return self

    def __exit__(self, *exc_info) -> None:
        live, self._live = self._live, None
        if live is None:
            return
        try:
            live.__exit__(*exc_info)
        except Exception:
            # Teardown failures (e.g. a broken terminal) must never mask an
            # exception already propagating out of the `with` body.
            if exc_info[0] is None:
                raise

    def on_event(self, event: RepoEvent) -> None:
        self.dashboard.on_event(event)
        now = self._clock()
        if event.kind == "done" or now - self._last >= self._min_interval:
            self._last = now
            if self._live is not None:
                self._live.refresh()
