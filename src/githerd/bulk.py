from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel

from githerd.gitops import pull
from githerd.journal import Journal, JournalEntry
from githerd.outcomes import Failed, Ok, Outcome
from githerd.runner import ProgressCb, parse_progress


class RepoEvent(BaseModel):
    repo: str
    kind: Literal["start", "progress", "done"]
    text: str = ""
    percent: int | None = None
    outcome: Outcome | None = None


log = logging.getLogger("githerd.bulk")

EventCb = Callable[[RepoEvent], None]
Operation = Callable[[Path, ProgressCb], Awaitable[Outcome]]


async def run_bulk(
    repos: list[Path],
    op: Operation,
    *,
    concurrency: int = 5,
    on_event: EventCb | None = None,
) -> dict[Path, Outcome]:
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    sem = asyncio.Semaphore(concurrency)

    def emit(event: RepoEvent) -> None:
        # A misbehaving callback must never affect a repo's real outcome.
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:
            log.exception("on_event callback raised for %s event of %s", event.kind, event.repo)

    async def one(repo: Path) -> tuple[Path, Outcome]:
        async with sem:
            emit(RepoEvent(repo=str(repo), kind="start"))

            def progress(line: str) -> None:
                parsed = parse_progress(line)
                emit(RepoEvent(
                    repo=str(repo), kind="progress", text=line.strip(),
                    percent=parsed[1] if parsed else None,
                ))

            try:
                outcome = await op(repo, progress)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one repo failing must not stop the others
                outcome = Failed(message=str(exc))
            emit(RepoEvent(repo=str(repo), kind="done", outcome=outcome))
            return repo, outcome

    return dict(await asyncio.gather(*(one(r) for r in repos)))


async def pull_repos(
    root: Path,
    repos: list[Path],
    *,
    concurrency: int = 5,
    on_event: EventCb | None = None,
) -> dict[Path, Outcome]:
    collected: dict[Path, Outcome] = {}

    def collect(event: RepoEvent) -> None:
        if event.kind == "done" and event.outcome is not None:
            collected[Path(event.repo)] = event.outcome
        if on_event is not None:
            on_event(event)

    try:
        return await run_bulk(repos, pull, concurrency=concurrency, on_event=collect)
    finally:
        # Runs on cancellation too: every repo that moved HEAD must be undoable.
        entries = [
            JournalEntry(repo=str(repo), op="pull",
                         before_head=o.before_head, after_head=o.after_head)
            for repo, o in collected.items()
            if isinstance(o, Ok) and o.before_head != o.after_head
        ]
        try:
            Journal(root).record(f"pull {len(repos)} repos", entries)
        except OSError:
            log.exception("failed to write journal for pull of %d repos", len(repos))
