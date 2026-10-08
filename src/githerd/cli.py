"""githerd command line interface.

Exit codes (``pull``):

* 0   every repo is up to date or was updated.
* 1   usage or environment problem (git missing, no repositories found).
* 2   at least one repo still needs attention (blocked, diverged, conflict, auth, failure).
      Typer/click also use 2 for a command-line usage error such as ``--timeout 0``.
* 130 interrupted with Ctrl+C (journalling of finished repos has already run).
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Iterator, Optional

import typer
from rich.console import Console
from rich.text import Text

from githerd.attention import exit_code_for, resolve_attention
from githerd.bulk import RepoEvent, pull_repos
from githerd.journal import Journal
from githerd.outcomes import describe, summarize
from githerd.repos import discover_repos, snapshot_all
from githerd.ui.confirm import confirm_destructive
from githerd.ui.dashboard import Dashboard, LiveDashboard
from githerd.ui.rows import render_status
from githerd.ui.theme import glyphs_for, make_console
from githerd.undo import undo_last

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


@app.command()
def status(root: RootOpt = None) -> None:
    """Show branch, sync state and local changes for every repo."""
    console = make_console()
    _, repos = _repos(root)
    snaps = asyncio.run(snapshot_all(repos))
    console.print(render_status(snaps, glyphs_for(console)))


@app.command()
def pull(
    root: RootOpt = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=1, help="Parallel repos.")] = 5,
    timeout: Annotated[float, typer.Option("--timeout", min=1, help="Seconds allowed per repo.")] = 300.0,
) -> None:
    """Pull every repo in parallel (fast-forward only).

    
    Exit codes:
      0    everything is up to date or was updated
      2    at least one repo still needs attention
      130  interrupted with Ctrl+C
    """
    console = make_console()
    glyphs = glyphs_for(console)
    base, repos = _repos(root)
    with _interrupts(console):
        if _interactive(console):
            snaps = asyncio.run(snapshot_all(repos))
            branches = {str(s.path): s.branch or "(detached)" for s in snaps}
            dashboard = Dashboard(repos, branches, glyphs, title="Pulling")
            with LiveDashboard(console, dashboard) as live:
                results = asyncio.run(
                    pull_repos(base, repos, concurrency=jobs, timeout=timeout, on_event=live.on_event)
                )
            console.print(Text(summarize(results.values()), style="subject"))
            results = resolve_attention(console, base, results, glyphs)
        else:
            width = max(len(r.name) for r in repos)

            def on_event(event: RepoEvent) -> None:
                if event.kind == "done" and event.outcome is not None:
                    typer.echo(f"{Path(event.repo).name:<{width}}  {describe(event.outcome)}")

            results = asyncio.run(
                pull_repos(base, repos, concurrency=jobs, timeout=timeout, on_event=on_event)
            )
            typer.echo(summarize(results.values()))
    code = exit_code_for(results.values())
    if code:
        raise typer.Exit(code=code)


@app.command()
def undo(root: RootOpt = None) -> None:
    """Undo the last operation (e.g. a bulk pull)."""
    console = make_console()
    glyphs = glyphs_for(console)
    base = _base(root)

    confirm = None
    if _interactive(console):
        def confirm(entry, current: str) -> bool:
            name = Path(entry.repo).name
            return confirm_destructive(
                console,
                command=f"git reset --keep {entry.before_head[:7]}  ({name})",
                repos=[name],
                glyphs=glyphs,
                detail=f"{name} has newer commits; they are dropped from the branch (kept in the reflog)",
            )

    options = {"confirm_moved": confirm} if confirm is not None else {}
    with _interrupts(console):
        op_set, items = asyncio.run(undo_last(base, Journal(base), **options))
    if op_set is None:
        console.print("Nothing to undo.")
        return
    console.print(Text(f"Undoing: {op_set.description}", style="subject"))
    styles = {
        "restored": (glyphs.ok, "ok"),
        "skipped": (glyphs.attn, "warn"),
        "failed": (glyphs.fail, "error"),
    }
    for item in items:
        glyph, style = styles[item.status]
        line = Text()
        line.append(f"{glyph} ", style=style)
        line.append(Path(item.repo).name, style="subject")
        line.append(f"  {item.status}", style=style)
        if item.detail:
            line.append(f": {item.detail}", style="dim")
        console.print(line)


if __name__ == "__main__":
    app()
