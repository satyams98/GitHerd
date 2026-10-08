from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Iterable

from rich.console import Console
from rich.text import Text

from githerd.diffs import FileDiff, diffs_for
from githerd.bulk import HeadMove, record_pulls
from githerd.gitops import head_and_branch, pull
from githerd.interactive import run_git_interactive
from githerd.outcomes import BlockedDirty, Outcome, describe
from githerd.recover import stash_and_pull
from githerd.textsafe import clean_message
from githerd.ui import keys
from githerd.ui.cards import actions_for, needs_attention, render_card
from githerd.ui.diffview import run_diff_viewer
from githerd.ui.theme import Glyphs

STASH_NOTE = "  if you had local changes, check 'git stash list'"

Viewer = Callable[[list[FileDiff]], object]
Heads = dict[Path, HeadMove]  # repo -> (first HEAD before, last HEAD after, branch it was on)


def exit_code_for(outcomes: Iterable[Outcome]) -> int:
    return 2 if any(needs_attention(o) for o in outcomes) else 0


def _head(repo: Path) -> tuple[str, str | None]:
    """``(HEAD sha, branch or None if detached)`` in one git spawn; ``("", None)`` if unreadable."""
    try:
        return asyncio.run(head_and_branch(repo)) or ("", None)
    except Exception:
        return "", None


def _record_move(heads: Heads, repo: Path, before: str, after: str, branch: str | None) -> None:
    first, _, first_branch = heads[repo] if repo in heads else (before, after, branch)
    heads[repo] = (first, after, first_branch)


def _record_moves(root: Path, description: str, heads: Heads) -> None:
    """Journal every repo whose HEAD moved; journalling must never crash a finished action."""
    record_pulls(root, description, {}, heads)


def _mutate(repo: Path, heads: Heads, action: Callable[[], Outcome]) -> Outcome:
    """Run a mutating action, noting whether it moved HEAD (whatever outcome it returns)."""
    before, branch = _head(repo)
    try:
        return action()
    finally:  # also on Ctrl+C, so a half-finished pull stays undoable
        _record_move(heads, repo, before, _head(repo)[0], branch)


def _resolve_one(
    console: Console, repo: Path, outcome: Outcome, glyphs: Glyphs,
    read_key: Callable[[], str], viewer: Viewer, heads: Heads,
) -> Outcome:
    def authenticate() -> Outcome:
        run_git_interactive(repo, "fetch")  # git talks to the user; we never see credentials
        return asyncio.run(pull(repo))

    while needs_attention(outcome):
        console.print(render_card(repo.name, outcome, glyphs))
        action = keys.choose_action(console, actions_for(outcome), glyphs, read_key)
        if action.id == "skip":
            break
        if action.id == "diff" and isinstance(outcome, BlockedDirty):
            try:
                viewer(asyncio.run(diffs_for(repo, outcome.files)))
            except Exception as exc:  # best-effort aid: e.g. prompt_toolkit raises its own error without a console
                console.print(Text(
                    f"  diff viewer unavailable: {clean_message(str(exc)) or type(exc).__name__}", style="dim",
                ))
            continue
        if action.id == "stash_pull":
            try:
                outcome = _mutate(repo, heads, lambda: asyncio.run(stash_and_pull(repo)))
            except BaseException:  # Ctrl+C: any stashed work may or may not have been put back
                console.print(Text(STASH_NOTE, style="dim"))
                raise
        elif action.id == "auth":
            outcome = _mutate(repo, heads, authenticate)
        else:  # retry
            outcome = _mutate(repo, heads, lambda: asyncio.run(pull(repo)))
        console.print(Text(f"  {describe(outcome)}", style="dim"))
    return outcome


def resolve_attention(
    console: Console,
    root: Path,
    results: dict[Path, Outcome],
    glyphs: Glyphs,
    *,
    read_key: Callable[[], str] | None = None,
    viewer: Viewer = run_diff_viewer,
) -> dict[Path, Outcome]:
    reader = read_key or keys.read_key  # looked up at call time so it can be patched
    final = dict(results)  # never mutate the caller's mapping
    heads: Heads = {}
    try:
        for repo, outcome in results.items():
            if needs_attention(outcome):
                final[repo] = _resolve_one(console, repo, outcome, glyphs, reader, viewer, heads)
    finally:  # a Ctrl+C part-way through still journals what was already done
        moved = sum(1 for before, after, _ in heads.values() if before and after and before != after)
        _record_moves(root, f"resolve {moved} repos", heads)
    return final
