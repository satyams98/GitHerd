from __future__ import annotations

import asyncio
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


EventCb = Callable[[RepoEvent], None]
Operation = Callable[[Path, ProgressCb], Awaitable[Outcome]]


async def run_bulk(
    repos: list[Path],
    op: Operation,
    *,
    concurrency: int = 5,
    on_event: EventCb | None = None,
) -> dict[Path, Outcome]:
    sem = asyncio.Semaphore(concurrency)
    emit: EventCb = on_event or (lambda event: None)

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
    results = await run_bulk(repos, pull, concurrency=concurrency, on_event=on_event)
    entries = [
        JournalEntry(repo=str(repo), op="pull",
                     before_head=o.before_head, after_head=o.after_head)
        for repo, o in results.items()
        if isinstance(o, Ok) and o.before_head != o.after_head
    ]
    Journal(root).record(f"pull {len(repos)} repos", entries)
    return results
