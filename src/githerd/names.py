"""Display labels for repositories: the directory name, or a path when names collide."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from githerd.textsafe import safe_path


def _relative(repo: Path, root: Path) -> str:
    """Posix-style path of ``repo`` below ``root``; the absolute path (forward slashes) otherwise."""
    try:
        relative = repo.relative_to(root).as_posix()
    except ValueError:
        return repo.as_posix()
    return repo.as_posix() if relative == "." else relative


def display_names(repos: list[Path], root: Path) -> dict[Path, str]:
    """One label per repo: its directory name when that is unique among ``repos``.

    Every repo that shares its directory name with another gets its path relative to
    ``root`` instead (forward slashes; the absolute path when it is not under ``root``), so
    two ``api`` repos under different parents can be told apart. Labels are safe to print.
    """
    counts = Counter(repo.name for repo in repos)
    return {
        repo: safe_path(repo.name if counts[repo.name] == 1 else _relative(repo, root))
        for repo in repos
    }
