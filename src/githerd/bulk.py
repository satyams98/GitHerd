from __future__ import annotations

import asyncio
import logging
import math
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Awaitable, Callable, Literal, Mapping

from pydantic import BaseModel

from githerd.gitops import head_and_branch, head_sync, pull
from githerd.journal import Journal, JournalEntry
from githerd.outcomes import AuthRequired, Conflict, Failed, NetworkError, Ok, Outcome
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
HeadMove = tuple[str, str, str | None]  # (HEAD before, HEAD after, branch)

# Overall budget (seconds) for re-reading HEADs after a pull run; see _moved_without_outcome.
AFTER_READ_DEADLINE = 30.0
# Outcomes of a pull that may have moved HEAD before failing; UpToDate, BlockedDirty and
# Diverged cannot have, so their repos are never re-read.
_MAYBE_MOVED = (Failed, NetworkError, AuthRequired, Conflict)


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
        JournalEntry(repo=str(repo), op="pull", before_head=o.before_head,
                     after_head=o.after_head, branch=o.branch)
        for repo, o in outcomes.items()
        if isinstance(o, Ok) and o.before_head != o.after_head
    ]


def move_entries(moves: Mapping[Path, HeadMove]) -> list[JournalEntry]:
    """Journal entries for repos whose HEAD verifiably moved (both ends known and different)."""
    return [
        JournalEntry(repo=str(repo), op="pull", before_head=before, after_head=after, branch=branch)
        for repo, (before, after, branch) in moves.items()
        if before and after and before != after
    ]


def record_pulls(
    root: Path,
    description: str,
    outcomes: dict[Path, Outcome],
    moves: Mapping[Path, HeadMove] | None = None,
) -> None:
    """Journal every pull that moved HEAD; journalling must never crash a finished pull.

    ``Ok`` outcomes carry their own heads. ``moves`` adds repos whose HEAD moved without an
    ``Ok`` outcome (timed out, cancelled, failed after moving); an outcome wins over a move.
    """
    try:
        entries = pull_entries(outcomes)
        if moves:
            seen = {e.repo for e in entries}
            entries += [e for e in move_entries(moves) if e.repo not in seen]
        Journal(root).record(description, entries)
    except Exception:
        log.exception("failed to write journal for %s", description)


def _moved_without_outcome(
    baseline: Mapping[Path, tuple[str, str | None]],
    collected: Mapping[Path, Outcome],
    concurrency: int,
) -> dict[Path, HeadMove]:
    """Re-read HEAD (synchronously: this runs while cancelling) of repos that may have moved.

    Only repos whose outcome is missing (cancelled) or one of Failed / NetworkError /
    AuthRequired / Conflict are read: an ``Ok`` carries its own heads, and UpToDate,
    BlockedDirty and Diverged cannot have moved HEAD. The whole loop is bounded by
    ``AFTER_READ_DEADLINE`` seconds; repos not read by then are logged and left unchecked.
    """
    todo = [
        r for r in baseline
        if collected.get(r) is None or isinstance(collected[r], _MAYBE_MOVED)
    ]
    if not todo:
        return {}

    def after_head(repo: Path) -> str | None:
        try:
            return head_sync(repo)
        except Exception:
            log.warning("could not re-read HEAD of %s", repo, exc_info=True)
            return None

    pool = ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(todo))))
    try:
        futures = {repo: pool.submit(after_head, repo) for repo in todo}
        _, pending = wait(futures.values(), timeout=AFTER_READ_DEADLINE)
    finally:
        # Never wait for stragglers: each git read has its own timeout and ends by itself.
        pool.shutdown(wait=False, cancel_futures=True)
    if pending:
        n = len(pending)
        log.warning(
            "re-reading HEADs exceeded %gs; %d repo%s not checked (a pull that moved HEAD there "
            "may be missing from the undo journal)", AFTER_READ_DEADLINE, n, "" if n == 1 else "s",
        )
    moves: dict[Path, HeadMove] = {}
    for repo, future in futures.items():
        if future not in pending:
            after = future.result()
            if after and after != baseline[repo][0]:
                moves[repo] = (baseline[repo][0], after, baseline[repo][1])
    return moves


async def pull_repos(
    root: Path,
    repos: list[Path],
    *,
    concurrency: int = 5,
    on_event: EventCb | None = None,
    timeout: float | None = None,
) -> dict[Path, Outcome]:
    """Pull every repo (bounded concurrency) and journal each pull that moved HEAD.

    HEAD is compared before and after, so a pull that moved HEAD and then timed out, failed
    or was cancelled is still undoable. A failed pull that coincides with an unrelated HEAD
    move (the user committing concurrently) is journaled as a pull; undo only ever resets
    when HEAD is still at the recorded after_head.
    """
    timeout = _validate_timeout(timeout)  # fail fast, before any work or journalling
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    collected: dict[Path, Outcome] = {}
    baseline: dict[Path, tuple[str, str | None]] = {}  # repo -> (HEAD, branch) before pulling

    def collect(event: RepoEvent) -> None:
        if event.kind == "done" and event.outcome is not None:
            collected[Path(event.repo)] = event.outcome
        if on_event is not None:
            on_event(event)

    async def read_then_pull(repo: Path, progress: ProgressCb) -> Outcome:
        # Lazy: read inside this repo's concurrency slot, so the first repo starts at once
        # and a hung read is covered by the per-repo timeout. A repo whose read never
        # completed (error, timeout, cancellation) is simply not compared afterwards.
        try:
            got = await head_and_branch(repo)
        except Exception:  # an unreadable repo just goes unjournaled; the pull still runs
            log.warning("could not read HEAD of %s before pulling", repo, exc_info=True)
            got = None
        if got is not None:
            baseline[repo] = got
        return await pull(repo, progress)

    try:
        return await run_bulk(repos, read_then_pull, concurrency=concurrency, on_event=collect, timeout=timeout)
    finally:
        # Runs on cancellation too: every repo that moved HEAD must be undoable, whatever
        # the outcome (a pull can move HEAD and then time out or fail). No awaits in here.
        try:
            moves = _moved_without_outcome(baseline, collected, concurrency)
        except Exception:
            log.exception("failed to compare HEADs after pulling")
            moves = {}
        record_pulls(root, f"pull {len(repos)} repos", collected, moves)
