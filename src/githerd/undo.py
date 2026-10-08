from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from githerd.journal import Journal, JournalEntry, OpSet
from githerd.runner import run_git

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


async def undo_last(
    root: Path,
    journal: Journal,
    *,
    confirm_moved: Callable[[JournalEntry, str], bool] | None = None,
) -> tuple[OpSet | None, list[UndoItem]]:
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
        head_res = await run_git(repo, "rev-parse", "HEAD")
        if not head_res.ok:
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail="repo not found or not a git repository"))
            continue
        current = head_res.stdout.strip()
        moved = current != entry.after_head
        if moved and not _confirmed(confirm_moved, entry, current):
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail="repo has moved since this operation; not undone"))
            retryable.append(entry)
            continue
        res = await run_git(repo, *reverse, entry.before_head)
        if res.ok:
            suffix = " (newer commits dropped)" if moved else ""
            items.append(UndoItem(repo=entry.repo, status="restored",
                                  detail=f"back to {entry.before_head[:7]}{suffix}"))
            restored_any = True
            if entry.op != "undo":  # undoing an undo is terminal; don't journal it again
                reversals.append(JournalEntry(
                    repo=entry.repo, op="undo",
                    before_head=current, after_head=entry.before_head,
                ))
        else:
            items.append(UndoItem(repo=entry.repo, status="failed",
                                  detail=res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "git reset failed"))
            retryable.append(entry)
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
