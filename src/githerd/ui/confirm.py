from __future__ import annotations

from typing import Callable

from rich.console import Console
from rich.text import Text

from githerd.textsafe import safe_path
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
    if not repos:  # nothing would be affected: never ask the user to approve it
        return False
    heading = Text()
    heading.append(f"{glyphs.attn} destructive: ", style="warn")
    heading.append(safe_path(command), style="subject")
    console.print(heading)
    names = ", ".join(safe_path(r) for r in repos[:5])
    if len(repos) > 5:
        names += f" {glyphs.ellipsis} +{len(repos) - 5} more"
    console.print(Text(f"  affects {len(repos)} repo{'' if len(repos) == 1 else 's'}: {names}", style="dim"))
    if detail:
        console.print(Text(f"  {safe_path(detail)}", style="dim"))
    try:
        answer = read_line("  proceed? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        console.print()  # the prompt left the cursor mid-line; start following output fresh
        return False
    return answer.strip().lower() in {"y", "yes"}
