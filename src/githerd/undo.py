from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from githerd.gitops import head_and_branch
from githerd.journal import Journal, JournalEntry, OpSet
from githerd.runner import run_git
from githerd.textsafe import clean_message

# Ops whose reversal is "move HEAD back to before_head, keeping local changes".
# Later plans register their own reversals (e.g. commit -> reset --soft) here.
_REVERSE_ARGS: dict[str, tuple[str, ...]] = {
    "pull": ("reset", "--keep"),
    "undo": ("reset", "--keep"),
}


log = logging.getLogger("githerd.undo")


class UndoItem(BaseModel):
    repo: str
    status: Literal["restored", "skipped", "failed"]
    detail: str = ""


def _confirmed(
    confirm_moved: Callable[[JournalEntry, str], bool] | None,
    entry: JournalEntry,
    current: str,
) -> bool:
    """Ask once whether to undo a moved repo; a missing or failing callback means no."""
    if confirm_moved is None:
        return False
    try:
        return bool(confirm_moved(entry, current))
    except Exception:
        # A broken prompt must never crash the undo or stop the other repos.
        log.exception("confirm_moved failed for %s; treating as declined", entry.repo)
        return False


async def _newer_commit_note(repo: Path, entry: JournalEntry, current: str) -> str:
    """Describe how many commits a confirmed reset drops, e.g. ' (2 newer commits dropped)'."""
    res = await run_git(repo, "rev-list", "--count", f"{entry.after_head}..{current}")
    try:
        count = int(res.stdout.strip()) if res.ok else 0
    except ValueError:
        count = 0
    if count < 1:
        return " (newer commits dropped)"
    return f" ({count} newer commit{'' if count == 1 else 's'} dropped)"


async def undo_last(
    root: Path,
    journal: Journal,
    *,
    confirm_moved: Callable[[JournalEntry, str], bool] | None = None,
) -> tuple[OpSet | None, list[UndoItem]]:
    """Reverse the most recent undoable operation set, repo by repo.

    When an entry records the ``branch`` the operation changed, the repo must still be on
    that branch (a detached HEAD never matches). This gate runs FIRST, before any other
    check: otherwise the repo is skipped without any prompt, stays retryable, and nothing
    is reset, even if its HEAD still equals ``after_head`` (a new branch cut at the pulled
    commit) or ``before_head`` (a hotfix branch cut at the old commit). Entries from old
    journals have no branch and skip this check.

    A repo whose HEAD no longer equals the recorded ``after_head`` has "moved". If it
    simply moved forward on the same history (``after_head`` is an ancestor of HEAD),
    ``confirm_moved(entry, current_head)`` is asked whether to reset it anyway, dropping
    the newer commits. Moved repos on different history (another branch, a rebase, a
    reset elsewhere) are skipped without prompting and stay retryable; a repo already
    at ``before_head`` is skipped as done and is not retried.

    ``confirm_moved`` is a SYNCHRONOUS callback: it blocks the event loop while it
    runs, so a UI caller must supply a non-blocking callback (e.g. one that reads a
    decision collected beforehand) or run ``undo_last`` in a worker thread. It is only
    called for repos that moved forward on the same history, and must return a bool.
    Exceptions it raises are logged and treated as "declined" (BaseExceptions such as
    KeyboardInterrupt propagate).
    """
    op_set = journal.last_undoable()
    if op_set is None:
        return None, []
    items: list[UndoItem] = []
    reversals: list[JournalEntry] = []
    retryable: list[JournalEntry] = []  # failed or moved: worth another attempt later
    restored_any = False
    for entry in op_set.entries:
        repo = Path(entry.repo)
        reverse = _REVERSE_ARGS.get(entry.op)
        if reverse is None:
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail=f"no undo available for '{entry.op}'"))
            continue
        head = await head_and_branch(repo)  # one spawn: sha and unambiguous branch name
        if head is None:
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail="repo not found or not a git repository"))
            continue
        current, now_on = head
        if entry.branch is not None and now_on != entry.branch:
            # The branch gate comes first: a branch cut at the pulled (or the previous)
            # commit is not what was pulled, and resetting it would move the wrong
            # branch. Whatever else is true of this repo (even "already at before_head")
            # says nothing about the recorded branch, so this stays retryable. No prompt.
            recorded = clean_message(entry.branch)
            if now_on is None:
                detail = (f"repo is on a detached HEAD but the operation was on branch "
                          f"'{recorded}'; not undone")
            else:
                detail = (f"repo is on branch '{clean_message(now_on)}' but the operation was on "
                          f"'{recorded}'; not undone")
            items.append(UndoItem(repo=entry.repo, status="skipped", detail=detail))
            retryable.append(entry)
            continue
        moved = current != entry.after_head
        suffix = ""
        if moved and current == entry.before_head:
            # Already reset there by the user: nothing to undo, nothing to retry.
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail=f"already at {current[:7]}"))
            continue
        if moved:
            # Exit 1 = not an ancestor; anything else (e.g. object gone after a gc)
            # is equally "can't prove it is the same history".
            ancestry = await run_git(repo, "merge-base", "--is-ancestor",
                                     entry.after_head, current)
            if ancestry.code != 0:
                items.append(UndoItem(repo=entry.repo, status="skipped",
                                      detail="repo is on different history than this operation; not undone"))
                retryable.append(entry)
                continue
            if not _confirmed(confirm_moved, entry, current):
                items.append(UndoItem(repo=entry.repo, status="skipped",
                                      detail="repo has moved since this operation; not undone"))
                retryable.append(entry)
                continue
            suffix = await _newer_commit_note(repo, entry, current)
        res = await run_git(repo, *reverse, entry.before_head)
        if res.ok:
            items.append(UndoItem(repo=entry.repo, status="restored",
                                  detail=f"back to {entry.before_head[:7]}{suffix}"))
            restored_any = True
            if entry.op != "undo":  # undoing an undo is terminal; don't journal it again
                reversals.append(JournalEntry(
                    repo=entry.repo, op="undo",
                    before_head=current, after_head=entry.before_head, branch=entry.branch,
                ))
        else:
            items.append(UndoItem(repo=entry.repo, status="failed",
                                  detail=clean_message(res.stderr.strip().splitlines()[-1]) if res.stderr.strip() else "git reset failed"))
            retryable.append(entry)
    if not restored_any and not retryable:
        # Every entry was a permanent skip: nothing to retry, so close the op set
        # instead of leaving it as the "last" one and hiding older op sets.
        try:
            journal.mark_undone(op_set.id)
        except OSError:
            log.exception("failed to close op set %s", op_set.id)
    if restored_any:
        try:
            journal.mark_undone(op_set.id)
            journal.record(f"undo: {op_set.description}", reversals)
            # Recorded last so the next `undo` retries the leftovers first.
            journal.record(f"{op_set.description} (not yet undone)", retryable)
        except OSError:
            # The resets already happened; never turn a done undo into a crash.
            log.exception("failed to update journal after undoing %s", op_set.id)
    return op_set, items
