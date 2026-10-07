from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import BaseModel, Field

from githerd.outcomes import FileChange
from githerd.runner import GitError, run_git

DEFAULT_IGNORE = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build"}
)


def discover_repos(
    root: Path, *, max_depth: int = 4, ignore: frozenset[str] = DEFAULT_IGNORE
) -> list[Path]:
    root = root.resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        if ".git" in dirnames or ".git" in filenames:  # dir, or file for worktrees
            found.append(current)
            dirnames[:] = []
            continue
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d for d in dirnames if d not in ignore and not d.startswith(".")
        )
    return sorted(found)


def parse_status_v2(raw: str) -> dict:
    """Parse `git status --porcelain=v2 --branch -z` output."""
    branch: str | None = None
    head = ""
    upstream: str | None = None
    ahead = behind = 0
    dirty: list[FileChange] = []
    fields = raw.split("\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        if field.startswith("# branch.oid "):
            oid = field.split(" ", 2)[2]
            head = "" if oid == "(initial)" else oid
        elif field.startswith("# branch.head "):
            name = field.split(" ", 2)[2]
            branch = None if name == "(detached)" else name
        elif field.startswith("# branch.upstream "):
            upstream = field.split(" ", 2)[2]
        elif field.startswith("# branch.ab "):
            _, _, plus, minus = field.split(" ")
            ahead, behind = int(plus[1:]), int(minus[1:])
        elif field.startswith("1 "):
            parts = field.split(" ", 8)
            dirty.append(FileChange(status=parts[1].replace(".", " "), path=parts[8]))
        elif field.startswith("2 "):
            parts = field.split(" ", 9)
            dirty.append(FileChange(status=parts[1].replace(".", " "), path=parts[9]))
            i += 1  # skip the separate original-path field
        elif field.startswith("u "):
            parts = field.split(" ", 10)
            dirty.append(FileChange(status=parts[1], path=parts[10]))
        elif field.startswith("? "):
            dirty.append(FileChange(status="??", path=field[2:]))
    return {
        "branch": branch, "head": head, "upstream": upstream,
        "ahead": ahead, "behind": behind, "dirty": dirty,
    }


class RepoSnapshot(BaseModel):
    path: Path
    name: str
    branch: str | None = None
    head: str = ""
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty: list[FileChange] = Field(default_factory=list)
    last_commit: str = ""
    error: str = ""

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty)


async def snapshot(repo: Path) -> RepoSnapshot:
    res = await run_git(
        repo, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal"
    )
    if not res.ok:
        raise GitError(res.stderr.strip() or "git status failed")
    info = parse_status_v2(res.stdout)
    last = await run_git(repo, "log", "-1", "--format=%s")
    return RepoSnapshot(
        path=repo, name=repo.name,
        last_commit=last.stdout.strip() if last.ok else "", **info,
    )


async def snapshot_all(repos: list[Path], concurrency: int = 8) -> list[RepoSnapshot]:
    sem = asyncio.Semaphore(concurrency)

    async def one(repo: Path) -> RepoSnapshot:
        async with sem:
            try:
                return await snapshot(repo)
            except GitError as exc:
                return RepoSnapshot(path=repo, name=repo.name, error=str(exc))

    return list(await asyncio.gather(*(one(r) for r in repos)))
