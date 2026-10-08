from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from rich.console import Console
from rich.text import Text

from githerd.asyncutil import run_coro_sync
from githerd.diffs import FileDiff, file_diff
from githerd.bulk import HeadMove, record_pulls, validate_timeout
from githerd.gitops import FETCH_COMMAND, PULL_FF_COMMAND, head_and_branch, pull
from githerd.interactive import run_git_interactive
from githerd.outcomes import BlockedDirty, Failed, FileChange, Outcome, describe
from githerd.recover import STASH_PULL_COMMAND, auto_stash_present, stash_and_pull, stash_tip
from githerd.textsafe import clean_message, safe_path
from githerd.ui import keys
from githerd.ui.cards import actions_for, needs_attention, render_card
from githerd.ui.diffview import run_lazy_diff_viewer
from githerd.ui.theme import Glyphs

STASH_NOTE = "  if you had local changes, check 'git stash list'"
STASH_KEPT_SUFFIX = " (your local changes are still in 'git stash')"

# A viewer receives the dirty files and a SYNC loader for one file's diff; it should call the
# loader only for the file it is showing (the diffs are loaded lazily, one git spawn each).
Loader = Callable[[FileChange], FileDiff]
Viewer = Callable[[list[FileChange], Loader], object]
Heads = dict[Path, HeadMove]  # repo -> (first HEAD before, last HEAD after, branch it was on)


def exit_code_for(outcomes: Iterable[Outcome]) -> int:
    return 2 if any(needs_attention(o) for o in outcomes) else 0


def _default_viewer(changes: list[FileChange], loader: Loader) -> object:
    return run_lazy_diff_viewer(changes, loader)  # looked up at call time so it can be patched


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


async def _bounded(
    work: Awaitable[Outcome], timeout: float | None,
    after_timeout: Callable[[], Awaitable[str]] | None = None,
) -> Outcome:
    """Await ``work``; with a truthy ``timeout`` an overrun is cancelled and reported as ``Failed``.

    ``None`` and ``0`` mean no timeout, like the bulk pull. Cancellation kills the git
    process tree, and ``work`` gets to run its own clean-up (a stash is put back).
    ``after_timeout`` runs only after our own deadline fired; the text it returns is
    appended to the ``Failed`` message.
    """
    if not timeout:
        return await work
    limiter = asyncio.timeout(timeout)
    try:
        async with limiter:
            return await work
    except TimeoutError:
        if limiter.expired():  # our deadline fired, not some TimeoutError inside the work
            message = f"timed out after {timeout:g}s"
            if after_timeout is not None:
                message += await after_timeout()
            return Failed(message=message)
        raise


def _note(console: Console, text: str) -> None:
    console.print(Text(text, style="dim"))


def _command_note(console: Console, command: str) -> None:
    """Quietly show the real git command an action is about to run."""
    _note(console, f"    $ {command}")


def _stash_tip_before(repo: Path) -> str | None:
    """``refs/stash`` before an action, or ``None`` when it cannot be read (never raises)."""
    try:
        return asyncio.run(asyncio.wait_for(stash_tip(repo), 10))
    except Exception:  # an unreadable tip only makes the later check less exact
        return None


def _resolve_one(
    console: Console, repo: Path, outcome: Outcome, glyphs: Glyphs,
    read_key: Callable[[], str], viewer: Viewer, heads: Heads,
    *, more: bool = False, timeout: float | None = None, label: str | None = None,
) -> tuple[Outcome, bool]:
    """Offer actions for one repo until it is settled; returns ``(outcome, stop)``.

    ``more`` says other repos still need attention (it enables ``skip all``); ``stop`` is
    True when the user chose ``skip all``, so the caller must leave the rest untouched.
    ``label`` is how the repo is named on screen (default: its directory name).
    """
    label = safe_path(label if label is not None else repo.name)

    def authenticate() -> Outcome:
        run_git_interactive(repo, "fetch")  # git talks to the user (not timed); we never see credentials
        return asyncio.run(_bounded(pull(repo), timeout))

    while needs_attention(outcome):
        console.print(render_card(label, outcome, glyphs))
        action = keys.choose_action(console, actions_for(outcome, more=more), glyphs, read_key)
        if action.id == "skip_all":
            return outcome, True
        if action.id == "skip":
            break
        if action.id == "diff" and isinstance(outcome, BlockedDirty):
            try:
                # The viewer calls the loader from inside prompt_toolkit's running event loop,
                # where asyncio.run raises; run_coro_sync uses a fresh loop on a worker thread.
                viewer(outcome.files, lambda change: run_coro_sync(file_diff(repo, change)))
            except Exception as exc:  # best-effort aid: e.g. prompt_toolkit raises its own error without a console
                console.print(Text(
                    f"  diff viewer unavailable: {clean_message(str(exc)) or type(exc).__name__}", style="dim",
                ))
            continue
        if action.id == "stash_pull":
            _note(console, f"  stashing, pulling, restoring {label}...")
            _command_note(console, STASH_PULL_COMMAND)
            before_tip = _stash_tip_before(repo)

            async def stash_left() -> str:
                # A timeout cancelled the work, and the restore after the cancel may not have
                # happened: say so when a NEW auto-stash (made by this action, not an older
                # stranded one) is still there. Without a baseline any auto-stash counts.
                present = (
                    await auto_stash_present(repo) if before_tip is None
                    else await auto_stash_present(repo, before_tip)
                )
                if not present:
                    return ""
                _note(console, STASH_NOTE)
                return STASH_KEPT_SUFFIX

            try:
                outcome = _mutate(
                    repo, heads,
                    lambda: asyncio.run(_bounded(stash_and_pull(repo), timeout, stash_left)),
                )
            except BaseException:  # Ctrl+C: any stashed work may or may not have been put back
                _note(console, STASH_NOTE)
                raise
        elif action.id == "auth":
            _note(console, f"  running git fetch for {label} (git may ask for credentials)...")
            _command_note(console, FETCH_COMMAND)
            outcome = _mutate(repo, heads, authenticate)
        else:  # retry
            _note(console, f"  pulling {label}...")
            _command_note(console, PULL_FF_COMMAND)
            outcome = _mutate(repo, heads, lambda: asyncio.run(_bounded(pull(repo), timeout)))
        _note(console, f"  {describe(outcome)}")
    return outcome, False


def resolve_attention(
    console: Console,
    root: Path,
    results: dict[Path, Outcome],
    glyphs: Glyphs,
    *,
    read_key: Callable[[], str] | None = None,
    viewer: Viewer = _default_viewer,
    timeout: float | None = None,
    labels: dict[Path, str] | None = None,
) -> dict[Path, Outcome]:
    """Walk the repos that need attention, offering one card each, and return the final outcomes.

    ``timeout`` (seconds, ``None``/``0`` = none; negative or non-finite raises ``ValueError``
    up front) limits each pull or stash-and-pull action the user starts; the interactive
    authenticate hand-off is never timed. When a timeout leaves a NEW auto-stash made by
    that action in ``git stash``, the card says so. ``labels`` (repo path -> text) names
    repos on screen where the directory name is ambiguous. Choosing
    ``skip all`` leaves the current and every remaining repo as they are.

    This runs its own event loops (``asyncio.run``) and blocks on the keyboard, so it must
    not be called from a running event loop.
    """
    timeout = validate_timeout(timeout)  # fail fast: before any card, key read or journal write
    reader = read_key or keys.read_key  # looked up at call time so it can be patched
    final = dict(results)  # never mutate the caller's mapping
    heads: Heads = {}
    pending = [repo for repo, outcome in results.items() if needs_attention(outcome)]
    try:
        for index, repo in enumerate(pending):
            final[repo], stop = _resolve_one(
                console, repo, results[repo], glyphs, reader, viewer, heads,
                more=index + 1 < len(pending), timeout=timeout, label=(labels or {}).get(repo),
            )
            if stop:
                break
    finally:  # a Ctrl+C part-way through still journals what was already done
        moved = sum(1 for before, after, _ in heads.values() if before and after and before != after)
        _record_moves(root, f"resolve {moved} repos", heads)
    return final
