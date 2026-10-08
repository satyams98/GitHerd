from __future__ import annotations

from typing import Callable

from rich.console import Console
from rich.text import Text

from githerd.ui.theme import Glyphs


def confirm_destructive(
    console: Console,
    *,
    command: str,
    repos: list[str],
    glyphs: Glyphs,
    detail: str | None = None,
    read_line: Callable[[str], str] = input,
) -> bool:
    heading = Text()
    heading.append(f"{glyphs.attn} destructive: ", style="warn")
    heading.append(command, style="subject")
    console.print(heading)
    names = ", ".join(repos[:5])
    if len(repos) > 5:
        names += f" {glyphs.ellipsis} +{len(repos) - 5} more"
    console.print(Text(f"  affects {len(repos)} repo{'' if len(repos) == 1 else 's'}: {names}", style="dim"))
    if detail:
        console.print(Text(f"  {detail}", style="dim"))
    try:
        answer = read_line("  proceed? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}
