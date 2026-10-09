"""githerd command line interface.

Exit codes (``pull``):

* 0   every repo is up to date or was updated.
* 1   usage or environment problem (git missing, no repositories found).
* 2   at least one repo still needs attention (blocked, diverged, conflict, auth, failure).
      Typer/click also use 2 for a command-line usage error such as ``--timeout 0``.
      ``undo`` also exits 2 when any repo was skipped or failed to restore.
* 130 interrupted with Ctrl+C (journalling of finished repos has already run).
"""
from __future__ import annotations

import asyncio
import math
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Iterator, Optional

import typer
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from githerd.attention import exit_code_for, resolve_attention
from githerd.bulk import RepoEvent, pull_repos
from githerd.gitops import PULL_COMMAND, git_sync, head_and_branch_sync
from githerd.journal import Journal
from githerd.names import display_names
from githerd.outcomes import Ok, Outcome, describe, summarize
from githerd.repos import discover_repos, snapshot_all
from githerd.textsafe import clean_message, safe_path
from githerd.ui.confirm import confirm_destructive
from githerd.ui.dashboard import Dashboard, LiveDashboard
from githerd.ui.rows import render_status
from githerd.ui.theme import glyphs_for, make_console
from githerd.undo import UndoItem, undo_last

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Herd all your git repos with plain English.",
)


@app.callback()
def main() -> None:
    """Herd all your git repos with plain English."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # test runners swap in streams that cannot be reconfigured
    if shutil.which("git") is None:
        typer.echo("git executable not found; install Git for Windows")
        raise typer.Exit(code=1)


RootOpt = Annotated[
    Optional[Path],
    typer.Option("--root", "-r", help="Directory to scan (default: current directory)."),
]


def _base(root: Optional[Path]) -> Path:
    return (root or Path.cwd()).resolve()


def _repos(root: Optional[Path]) -> tuple[Path, list[Path]]:
    base = _base(root)
    repos = discover_repos(base)
    if not repos:
        typer.echo(f"No git repositories found under {base}")
        raise typer.Exit(code=1)
    return base, repos


@contextmanager
def _interrupts(console: Console) -> Iterator[None]:
    """Turn Ctrl+C into a tidy message and exit code 130 (after the engine's own cleanup)."""
    try:
        yield
    except KeyboardInterrupt:
        console.print("Interrupted.")
        raise typer.Exit(code=130)


def _interactive(console: Console) -> bool:
    """True when we can draw a live UI and read single key presses."""
    return console.is_terminal and sys.stdin.isatty()


UNDO_HINT = "undo available: githerd undo"
_BACK_TO = re.compile(r"^back to ([0-9a-fA-F]{4,40})(?![0-9a-fA-F])")


def _restored_sha(item: UndoItem) -> str | None:
    """The short sha a restored item went back to (from its ``back to <sha>`` detail), if any."""
    match = _BACK_TO.match(item.detail) if item.status == "restored" else None
    return match.group(1) if match else None


def _moved_any(results: dict[str, Outcome] | dict[Path, Outcome]) -> bool:
    """True when at least one repo was updated, i.e. its HEAD moved and the move was journaled."""
    return any(isinstance(o, Ok) and o.before_head != o.after_head for o in results.values())


def _finite_timeout(value: float) -> float:
    if not math.isfinite(value) or value < 1:
        raise typer.BadParameter("timeout must be a finite number of seconds >= 1")
    return value


def _undo_labels(base: Path, journaled: list[Path]) -> dict[Path, str]:
    """Labels for every repo under ``base`` and every ``journaled`` one (which may be gone or elsewhere)."""
    return display_names(list(dict.fromkeys([*discover_repos(base), *journaled])), base)


@app.command()
def status(root: RootOpt = None) -> None:
    """Show branch, sync state and local changes for every repo."""
    console = make_console()
    base, repos = _repos(root)
    with _interrupts(console):
        snaps = asyncio.run(snapshot_all(repos))
    labels = display_names(repos, base)
    if console.is_terminal:
        # Error lines are fitted to the terminal width so that none wraps.
        console.print(render_status(snaps, glyphs_for(console), labels=labels, width=console.width))
    else:
        # Piped output: one unwrapped, untruncated line per repo.
        console.print(
            render_status(snaps, glyphs_for(console), max_name_width=None, labels=labels),
            soft_wrap=True,
        )


@app.command()
def pull(
    root: RootOpt = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=1, help="Parallel repos.")] = 5,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=1, callback=_finite_timeout, help="Seconds allowed per repo."),
    ] = 300.0,
) -> None:
    """Pull every repo in parallel (fast-forward only).

    \b
    Exit codes:
      0    everything is up to date or was updated
      2    at least one repo still needs attention
      130  interrupted with Ctrl+C
    """
    console = make_console()
    glyphs = glyphs_for(console)
    base, repos = _repos(root)
    labels = display_names(repos, base)
    with _interrupts(console):
        if _interactive(console):
            snaps = asyncio.run(snapshot_all(repos))
            # A repo whose snapshot errored has no readable branch: "?" rather than "(detached)".
            branches = {str(s.path): s.branch or ("?" if s.error else "(detached)") for s in snaps}
            dashboard = Dashboard(
                repos, branches, glyphs, title="Pulling", command=PULL_COMMAND, labels=labels,
            )
            with LiveDashboard(console, dashboard) as live:
                results = asyncio.run(
                    pull_repos(base, repos, concurrency=jobs, timeout=timeout, on_event=live.on_event)
                )
            console.print(Text(summarize(results.values()), style="subject"))
            results = resolve_attention(console, base, results, glyphs, timeout=timeout, labels=labels)
            if _moved_any(results):
                console.print(Text(UNDO_HINT, style="dim"))
        else:
            width = max(cell_len(labels[r]) for r in repos)

            def on_event(event: RepoEvent) -> None:
                if event.kind == "done" and event.outcome is not None:
                    repo = Path(event.repo)
                    name = labels.get(repo) or safe_path(repo.name)
                    typer.echo(f"{name + ' ' * max(0, width - cell_len(name))}  {describe(event.outcome)}")

            results = asyncio.run(
                pull_repos(base, repos, concurrency=jobs, timeout=timeout, on_event=on_event)
            )
            typer.echo(summarize(results.values()))
            if _moved_any(results):
                typer.echo(UNDO_HINT)
    code = exit_code_for(results.values())
    if code:
        raise typer.Exit(code=code)


@app.command()
def undo(root: RootOpt = None) -> None:
    """Undo the last operation (e.g. a bulk pull).

    \b
    Exit codes:
      0  every repo was restored, or there was nothing to undo
      2  at least one repo was skipped or failed to restore
      130  interrupted with Ctrl+C
    """
    console = make_console()
    glyphs = glyphs_for(console)
    base = _base(root)
    journal = Journal(base)
    # ONE label map for everything undo prints (the prompt and the result lines): the repos under
    # the root plus the journaled ones, labelled exactly as pull and status label them, so a
    # duplicate name stays disambiguated even when only one of the duplicates was journaled.
    pending = journal.last_undoable()
    labels = _undo_labels(base, [Path(e.repo) for e in pending.entries]) if pending else {}

    confirm = None
    interactive = _interactive(console)
    if interactive:
        def confirm(entry, current: str) -> bool:
            repo = Path(entry.repo)
            name = labels.get(repo) or safe_path(repo.name)
            branch = entry.branch or (head_and_branch_sync(repo) or ("", None))[1] or "?"
            count = git_sync(repo, "rev-list", "--count", f"{entry.after_head}..{current}")
            if count is not None and count.isdigit() and int(count) >= 1:
                dropped = f"{count} newer commit{'' if int(count) == 1 else 's'}"
            else:
                dropped = "some newer commits"  # the count is only a courtesy; never block on it
            return confirm_destructive(
                console,
                command=f"git reset --keep {entry.before_head[:7]}  ({name})",
                repos=[name],
                glyphs=glyphs,
                read_line=lambda prompt: input(prompt),  # looked up per call, not bound at import
                detail=(f"{name} is on '{clean_message(branch)}'; {dropped} will be dropped "
                        "from it (kept in the reflog)"),
            )

    options = {"confirm_moved": confirm} if confirm is not None else {}
    with _interrupts(console):
        op_set, items = asyncio.run(undo_last(base, journal, **options))
    if op_set is None:
        console.print("Nothing to undo.")
        return
    # Piped output: one unwrapped line per item (a terminal wraps as it always did).
    wrap = {} if console.is_terminal else {"soft_wrap": True}
    console.print(Text(f"Undoing: {clean_message(op_set.description)}", style="subject"), **wrap)
    styles = {
        "restored": (glyphs.ok, "ok"),
        "skipped": (glyphs.attn, "warn"),
        "failed": (glyphs.fail, "error"),
    }
    paths = [Path(e.repo) for e in op_set.entries] + [Path(item.repo) for item in items]
    if any(path not in labels for path in paths):  # not what was journaled (a stand-in undo, say)
        labels = _undo_labels(base, paths)
    for item in items:
        glyph, style = styles[item.status]
        line = Text()
        line.append(f"{glyph} ", style=style)
        line.append(labels[Path(item.repo)], style="subject")
        line.append(f"  {item.status}", style=style)
        if item.detail:
            line.append(f": {clean_message(item.detail)}", style="dim")
        console.print(line, **wrap)
        sha = _restored_sha(item) if interactive else None
        if sha:
            console.print(Text(f"    $ git reset --keep {sha}", style="dim"), **wrap)
    if any(item.status != "restored" for item in items):
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
