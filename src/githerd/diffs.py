from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from githerd.outcomes import FileChange
from githerd.runner import GitError, run_git

MAX_DIFF_CHARS = 200_000
MAX_PARALLEL_DIFFS = 8

# Global git options: they must precede the subcommand.
_GIT_OPTS = ("-c", "core.quotePath=false", "--no-optional-locks", "--literal-pathspecs")
# Never run configured external diff drivers or textconv filters.
_DIFF = ("diff", "--no-color", "--no-ext-diff", "--no-textconv")


@dataclass(frozen=True)
class FileDiff:
    path: str
    status: str
    text: str


def _is_unsafe(path: str) -> bool:
    """True for absolute paths, drive/UNC paths and paths with a `..` segment."""
    if not path or path.startswith(("/", "\\")) or PureWindowsPath(path).drive:
        return True
    return ".." in path.replace("\\", "/").split("/")


async def _diff_text(repo: Path, change: FileChange) -> str:
    path = change.path
    if _is_unsafe(path):
        return f"(refusing to show {path}: unsafe path)"
    if path.endswith("/"):
        return f"(untracked directory: {path})"
    try:
        if change.status == "??":
            # Exit code 1 simply means "the files differ".
            res = await run_git(repo, *_GIT_OPTS, *_DIFF, "--no-index", "--", "/dev/null", path)
            if res.code == 1 and not res.stdout and res.stderr.strip():
                return f"(cannot show {path}: {res.stderr.strip()})"
            ok = res.code in (0, 1)
        else:
            res = await run_git(repo, *_GIT_OPTS, *_DIFF, "HEAD", "--", path)
            if not res.ok:  # repo without any commit yet
                staged = change.status[:1] not in (" ", "?", "")
                cached = ("--cached",) if staged else ()
                res = await run_git(repo, *_GIT_OPTS, *_DIFF, *cached, "--", path)
            ok = res.ok
    except (GitError, OSError) as exc:
        return f"(cannot show {path}: {str(exc) or type(exc).__name__})"
    if not ok:
        return f"(cannot show {path}: {res.stderr.strip() or 'git diff failed'})"
    return res.stdout


async def file_diff(repo: Path, change: FileChange) -> FileDiff:
    try:
        text = await _diff_text(repo, change)
    except Exception as exc:  # never raise; CancelledError is a BaseException and propagates
        text = f"(cannot show {change.path}: {str(exc) or type(exc).__name__})"
    if not text or text.isspace():
        text = f"(no textual changes for {change.path})"
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... (truncated)"
    return FileDiff(path=change.path, status=change.status, text=text)


async def diffs_for(repo: Path, changes: list[FileChange]) -> list[FileDiff]:
    gate = asyncio.Semaphore(MAX_PARALLEL_DIFFS)

    async def one(change: FileChange) -> FileDiff:
        async with gate:
            return await file_diff(repo, change)

    return list(await asyncio.gather(*(one(c) for c in changes)))
