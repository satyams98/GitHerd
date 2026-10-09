"""Display labels for repositories: the directory name, or a path when names collide."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from githerd.textsafe import safe_path

# Windows file systems treat API and api as one name, so they collide there.
_CASE_INSENSITIVE = sys.platform == "win32"


def _relative(repo: Path, root: Path) -> str:
    """Posix-style path of ``repo`` below ``root``; the absolute path (forward slashes) otherwise."""
    try:
        relative = repo.relative_to(root).as_posix()
    except ValueError:
        return repo.as_posix()
    return repo.as_posix() if relative == "." else relative


def _key(repo: Path) -> str:
    """What the reader sees as the name (after ``safe_path``), case-folded where case does not count."""
    name = safe_path(repo.name)
    return name.casefold() if _CASE_INSENSITIVE else name


def display_names(repos: list[Path], root: Path) -> dict[Path, str]:
    """One label per repo: its directory name when that is unique among ``repos``.

    Every repo that shares its directory name with another gets its path relative to
    ``root`` instead (forward slashes; the absolute path when it is not under ``root``), so
    two ``api`` repos under different parents can be told apart. Names count as shared when
    they look the same on screen (equal after ``safe_path``) and, on Windows, when they differ
    only in case. Labels are safe to print.
    """
    counts = Counter(_key(repo) for repo in repos)
    return {
        repo: safe_path(repo.name if counts[_key(repo)] == 1 else _relative(repo, root))
        for repo in repos
    }
