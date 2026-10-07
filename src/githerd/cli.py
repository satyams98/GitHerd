from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer

from githerd.bulk import RepoEvent, pull_repos
from githerd.journal import Journal
from githerd.outcomes import describe, summarize
from githerd.repos import discover_repos, snapshot_all
from githerd.undo import undo_last

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Herd all your git repos with plain English.",
)

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


@app.command()
def status(root: RootOpt = None) -> None:
    """Show branch, sync state and local changes for every repo."""
    _, repos = _repos(root)
    snaps = asyncio.run(snapshot_all(repos))
    width = max(len(s.name) for s in snaps)
    for s in snaps:
        if s.error:
            typer.echo(f"{s.name:<{width}}  error: {s.error}")
            continue
        branch = s.branch or "(detached)"
        sync = f"+{s.ahead}/-{s.behind}" if s.upstream else "no upstream"
        changes = f"{len(s.dirty)} changed" if s.dirty else "clean"
        typer.echo(f"{s.name:<{width}}  {branch:<16} {sync:<12} {changes}")


@app.command()
def pull(
    root: RootOpt = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=1, help="Parallel repos.")] = 5,
) -> None:
    """Pull every repo in parallel (fast-forward only)."""
    base, repos = _repos(root)
    width = max(len(r.name) for r in repos)

    def on_event(event: RepoEvent) -> None:
        if event.kind == "done" and event.outcome is not None:
            typer.echo(f"{Path(event.repo).name:<{width}}  {describe(event.outcome)}")

    results = asyncio.run(pull_repos(base, repos, concurrency=jobs, on_event=on_event))
    typer.echo(summarize(results.values()))


@app.command()
def undo(root: RootOpt = None) -> None:
    """Undo the last operation (e.g. a bulk pull)."""
    base = _base(root)
    op_set, items = asyncio.run(undo_last(base, Journal(base)))
    if op_set is None:
        typer.echo("Nothing to undo.")
        return
    typer.echo(f"Undoing: {op_set.description}")
    for item in items:
        typer.echo(f"{Path(item.repo).name}  {item.status}: {item.detail}")
