from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from githerd.gitops import pull
from githerd.outcomes import Conflict, Failed, Ok, Outcome, UpToDate
from githerd.runner import ProgressCb, git_env, run_git
from githerd.textsafe import clean_message

STASH_MESSAGE = "githerd: auto-stash before pull"
# What stash_and_pull runs, shown (dim) before the action; keep it in step with the code below.
STASH_PULL_COMMAND = "git stash push --include-untracked ; git pull --ff-only ; git stash pop"

log = logging.getLogger("githerd.recover")


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return clean_message(lines[-1]) if lines else "git failed"


def _describe(exc: BaseException) -> str:
    return clean_message(str(exc)) or type(exc).__name__


async def _conflicted_files(repo: Path) -> list[str]:
    res = await run_git(repo, "diff", "--name-only", "--diff-filter=U", "-z")
    return [p for p in res.stdout.split("\0") if p]


async def stash_tip(repo: Path) -> str:
    """The commit at the top of ``git stash`` (``refs/stash``), or ``""`` when there is no stash."""
    res = await run_git(repo, "rev-parse", "--verify", "--quiet", "refs/stash")
    return res.stdout.strip() if res.ok else ""


async def _new_auto_stash_on_top(repo: Path, before: str) -> bool:
    if await stash_tip(repo) in ("", before):
        return False  # no stash, or still the one that was on top before the action
    res = await run_git(repo, "stash", "list", "--max-count=1")  # the new top entry
    return res.ok and STASH_MESSAGE in res.stdout


async def auto_stash_present(repo: Path, before: str | None = None) -> bool:
    """True when our auto-stash is still listed in ``git stash``; never raises.

    Used after a timeout cancelled a stash-and-pull, where the restore may not have happened.
    A git failure (or a hung git: bounded to 10 s) counts as "not present".

    ``before`` is ``stash_tip`` as read BEFORE the action (``""`` for no stash). When given,
    only a stash made since then counts, so an older stranded one is not mistaken for ours;
    without it any stash with our message counts.
    """
    try:
        if before is not None:
            return await asyncio.wait_for(_new_auto_stash_on_top(repo, before), 10)
        res = await asyncio.wait_for(run_git(repo, "stash", "list"), 10)
    except Exception:  # includes TimeoutError and GitError; cancellation still propagates
        return False
    return res.ok and STASH_MESSAGE in res.stdout


async def _pop(repo: Path, on_success: Outcome) -> Outcome:
    """Re-apply our stash and return ``on_success`` if that works."""
    pop = await run_git(repo, "stash", "pop")
    if pop.ok:
        return on_success
    conflicts = await _conflicted_files(repo)
    if conflicts:
        return Conflict(files=conflicts, stash_kept=True)  # git keeps the stash on a conflicted pop
    return Failed(
        message="your stashed changes could not be re-applied; "
        f"they are kept in `git stash`: {_last_line(pop.stderr)}"
    )


def _sync_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Plain blocking git call; usable when the event loop is being torn down."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=git_env(), timeout=60, check=False,
    )


async def _restore_after_interrupt(repo: Path, before: str) -> None:
    """Best effort: put our stash back after Ctrl+C or cancellation, then let the caller re-raise.

    Our stash is on top of ``refs/stash`` exactly when the tip differs from what it was
    before we pushed, so an older stash of the user's is never popped by mistake.
    """
    try:
        tip = await stash_tip(repo)
        if tip and tip != before:
            await run_git(repo, "stash", "pop")
        return
    except BaseException:  # the async path was cancelled again or failed: go synchronous
        pass
    try:
        top = _sync_git(repo, "rev-parse", "--verify", "--quiet", "refs/stash")
        tip = top.stdout.strip() if top.returncode == 0 else ""
        if tip and tip != before:
            _sync_git(repo, "stash", "pop")
    except Exception as exc:  # expected failures (git missing, timeout): no traceback on the terminal
        log.warning(
            "could not restore stashed changes in %s: %s; check `git stash list`",
            repo, clean_message(str(exc)) or type(exc).__name__,
        )


async def stash_and_pull(repo: Path, on_progress: ProgressCb | None = None) -> Outcome:
    """Stash local work (including untracked files), pull, then re-apply it.

    Returns a typed outcome and never raises (cancellation still propagates). Local
    work is never discarded: it is either back in the working tree or still listed
    in ``git stash``. On Ctrl+C or cancellation the stash is popped on a best-effort
    basis before the interruption is re-raised.
    """
    stash_pending = False  # our stash exists and has not been popped yet
    before: str | None = None  # stash tip before we pushed; None until it is known
    settled = False  # the pop below has run, so there is nothing left to rescue
    try:
        before = await stash_tip(repo)
        stash = await run_git(repo, "stash", "push", "--include-untracked", "-m", STASH_MESSAGE)
        if not stash.ok:
            return Failed(message=f"could not stash local changes: {_last_line(stash.stderr)}")
        # Compare the stash tip rather than parsing git's wording; an older user
        # stash must never be popped when there was nothing to save.
        stash_pending = await stash_tip(repo) != before
        outcome = await pull(repo, on_progress)
        if not stash_pending:
            return outcome
        popped = await _pop(repo, outcome)  # on a failed pull this restores the tree as it was
        stash_pending = False
        settled = True
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
    except BaseException:  # Ctrl+C / cancellation: try to put the work back, then re-raise
        if before is not None and not settled:
            await _restore_after_interrupt(repo, before)
        raise
