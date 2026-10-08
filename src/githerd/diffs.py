from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from githerd.outcomes import FileChange
from githerd.runner import GitError, run_git

MAX_DIFF_CHARS = 200_000


@dataclass(frozen=True)
class FileDiff:
    path: str
    status: str
    text: str


async def _diff_text(repo: Path, change: FileChange) -> str:
    try:
        if change.status == "??":
            # Exit code 1 simply means "the files differ".
            res = await run_git(repo, "diff", "--no-index", "--no-color", "--", "/dev/null", change.path)
            ok = res.code in (0, 1)
        else:
            res = await run_git(repo, "diff", "--no-color", "HEAD", "--", change.path)
            if not res.ok:  # repo without any commit yet
                res = await run_git(repo, "diff", "--no-color", "--", change.path)
            ok = res.ok
    except GitError as exc:
        return f"(cannot show {change.path}: {exc})"
    if not ok:
        return f"(cannot show {change.path}: {res.stderr.strip() or 'git diff failed'})"
    return res.stdout


async def file_diff(repo: Path, change: FileChange) -> FileDiff:
    text = await _diff_text(repo, change)
    if not text.strip():
        text = f"(no textual changes for {change.path})"
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... (truncated)"
    return FileDiff(path=change.path, status=change.status, text=text)


async def diffs_for(repo: Path, changes: list[FileChange]) -> list[FileDiff]:
    return list(await asyncio.gather(*(file_diff(repo, c) for c in changes)))
