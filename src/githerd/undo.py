from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from githerd.journal import Journal, JournalEntry, OpSet
from githerd.runner import run_git

# Ops whose reversal is "move HEAD back to before_head, keeping local changes".
# Later plans register their own reversals (e.g. commit -> reset --soft) here.
_REVERSE_ARGS: dict[str, tuple[str, ...]] = {
    "pull": ("reset", "--keep"),
    "undo": ("reset", "--keep"),
}


class UndoItem(BaseModel):
    repo: str
    status: Literal["restored", "skipped", "failed"]
    detail: str = ""


async def undo_last(root: Path, journal: Journal) -> tuple[OpSet | None, list[UndoItem]]:
    op_set = journal.last_undoable()
    if op_set is None:
        return None, []
    items: list[UndoItem] = []
    reversals: list[JournalEntry] = []
    restored_any = False
    for entry in op_set.entries:
        repo = Path(entry.repo)
        reverse = _REVERSE_ARGS.get(entry.op)
        if reverse is None:
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail=f"no undo available for '{entry.op}'"))
            continue
        head = (await run_git(repo, "rev-parse", "HEAD")).stdout.strip()
        if head != entry.after_head:
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail="repo has moved since this operation; not undone"))
            continue
        res = await run_git(repo, *reverse, entry.before_head)
        if res.ok:
            items.append(UndoItem(repo=entry.repo, status="restored",
                                  detail=f"back to {entry.before_head[:7]}"))
            restored_any = True
            if entry.op != "undo":  # undoing an undo is terminal; don't journal it again
                reversals.append(JournalEntry(
                    repo=entry.repo, op="undo",
                    before_head=entry.after_head, after_head=entry.before_head,
                ))
        else:
            items.append(UndoItem(repo=entry.repo, status="failed",
                                  detail=res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "git reset failed"))
    if restored_any:
        journal.mark_undone(op_set.id)
        journal.record(f"undo: {op_set.description}", reversals)
    return op_set, items
