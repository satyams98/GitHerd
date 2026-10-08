from __future__ import annotations

from pathlib import Path

from githerd.gitops import pull
from githerd.outcomes import Conflict, Failed, Ok, Outcome, UpToDate
from githerd.runner import ProgressCb, run_git

STASH_MESSAGE = "githerd: auto-stash before pull"


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else "git failed"


def _describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


async def _conflicted_files(repo: Path) -> list[str]:
    res = await run_git(repo, "diff", "--name-only", "--diff-filter=U", "-z")
    return [p for p in res.stdout.split("\0") if p]


async def _stash_tip(repo: Path) -> str:
    res = await run_git(repo, "rev-parse", "--verify", "--quiet", "refs/stash")
    return res.stdout.strip() if res.ok else ""


async def _pop(repo: Path, on_success: Outcome) -> Outcome:
    """Re-apply our stash and return ``on_success`` if that works."""
    pop = await run_git(repo, "stash", "pop")
    if pop.ok:
        return on_success
    conflicts = await _conflicted_files(repo)
    if conflicts:
        return Conflict(files=conflicts)  # git keeps the stash on a conflicted pop
    return Failed(
        message="your stashed changes could not be re-applied; "
        f"they are kept in `git stash`: {_last_line(pop.stderr)}"
    )


async def stash_and_pull(repo: Path, on_progress: ProgressCb | None = None) -> Outcome:
    """Stash local work (including untracked files), pull, then re-apply it.

    Returns a typed outcome and never raises (cancellation still propagates). Local
    work is never discarded: it is either back in the working tree or still listed
    in ``git stash``.
    """
    stash_pending = False  # our stash exists and has not been popped yet
    try:
        before = await _stash_tip(repo)
        stash = await run_git(repo, "stash", "push", "--include-untracked", "-m", STASH_MESSAGE)
        if not stash.ok:
            return Failed(message=f"could not stash local changes: {_last_line(stash.stderr)}")
        # Compare the stash tip rather than parsing git's wording; an older user
        # stash must never be popped when there was nothing to save.
        stash_pending = await _stash_tip(repo) != before
        outcome = await pull(repo, on_progress)
        if not stash_pending:
            return outcome
        popped = await _pop(repo, outcome)  # on a failed pull this restores the tree as it was
        stash_pending = False
        if isinstance(popped, Failed) and isinstance(outcome, (Ok, UpToDate)):
            return Failed(message=f"pulled, but {popped.message}")
        return popped
    except Exception as exc:  # CancelledError is a BaseException and still propagates
        if not stash_pending:
            return Failed(message=_describe(exc))
        try:
            restored = (await run_git(repo, "stash", "pop")).ok
        except Exception:
            restored = False
        tail = "local changes restored" if restored else "your local changes are kept in `git stash`"
        return Failed(message=f"{_describe(exc)}; {tail}")
