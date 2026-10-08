from __future__ import annotations

import asyncio
import logging
import math
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


def _validate_timeout(timeout: float | None) -> float | None:
    """Return the effective timeout: ``None`` for "no timeout" (``None`` or ``0``)."""
    if timeout is None or timeout == 0:
        return None
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a positive number of seconds")
    return timeout


async def run_bulk(
    repos: list[Path],
    op: Operation,
    *,
    concurrency: int = 5,
    on_event: EventCb | None = None,
    timeout: float | None = None,
) -> dict[Path, Outcome]:
    """Run ``op`` over ``repos`` with bounded concurrency.

    ``timeout`` is a per-repo limit in seconds; an operation exceeding it is cancelled
    and reported as ``Failed``. ``None`` and ``0`` mean no timeout; a negative or
    non-finite value raises ``ValueError``.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    timeout = _validate_timeout(timeout)
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

            limiter = asyncio.timeout(timeout)  # timeout=None means no deadline
            try:
                async with limiter:
                    outcome = await op(repo, progress)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one repo failing must not stop the others
                # expired() is True only when *our* deadline fired, so an operation's
                # own TimeoutError is an ordinary failure, not the bulk timeout.
                if timeout is not None and limiter.expired():
                    outcome = Failed(message=f"timed out after {timeout:g}s")
                else:
                    outcome = Failed(message=str(exc) or type(exc).__name__)
            emit(RepoEvent(repo=str(repo), kind="done", outcome=outcome))
            return repo, outcome

    return dict(await asyncio.gather(*(one(r) for r in repos)))


def pull_entries(outcomes: dict[Path, Outcome]) -> list[JournalEntry]:
    return [
        JournalEntry(repo=str(repo), op="pull", before_head=o.before_head, after_head=o.after_head)
        for repo, o in outcomes.items()
        if isinstance(o, Ok) and o.before_head != o.after_head
    ]


def record_pulls(root: Path, description: str, outcomes: dict[Path, Outcome]) -> None:
    """Journal every pull that moved HEAD; journalling must never crash a finished pull."""
    try:
        Journal(root).record(description, pull_entries(outcomes))
    except Exception:
        log.exception("failed to write journal for %s", description)


async def pull_repos(
    root: Path,
    repos: list[Path],
    *,
    concurrency: int = 5,
    on_event: EventCb | None = None,
    timeout: float | None = None,
) -> dict[Path, Outcome]:
    timeout = _validate_timeout(timeout)  # fail fast, before any work or journalling
    collected: dict[Path, Outcome] = {}

    def collect(event: RepoEvent) -> None:
        if event.kind == "done" and event.outcome is not None:
            collected[Path(event.repo)] = event.outcome
        if on_event is not None:
            on_event(event)

    try:
        return await run_bulk(repos, pull, concurrency=concurrency, on_event=collect, timeout=timeout)
    finally:
        # Runs on cancellation too: every repo that moved HEAD must be undoable.
        record_pulls(root, f"pull {len(repos)} repos", collected)
