from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TextIO

from rich.console import Console
from rich.theme import Theme

# Semantic tokens only; named ANSI colours adapt to light and dark terminals.
THEME = Theme({
    "accent": "cyan",
    "ok": "green",
    "warn": "yellow",
    "error": "red",
    "subject": "bold",
})


@dataclass(frozen=True)
class Glyphs:
    ok: str
    attn: str
    fail: str
    running: str
    queued: str
    ahead: str
    behind: str
    prompt: str
    bar_on: str
    bar_off: str
    sep: str
    ellipsis: str


UNICODE_GLYPHS = Glyphs(
    ok="✓", attn="!", fail="✕", running="◌", queued="·", ahead="↑", behind="↓",
    prompt="›", bar_on="━", bar_off="─", sep="·", ellipsis="…",
)
ASCII_GLYPHS = Glyphs(
    ok="+", attn="!", fail="x", running="*", queued=".", ahead="^", behind="v",
    prompt=">", bar_on="#", bar_off="-", sep="|", ellipsis="...",
)


def glyphs_for(console: Console) -> Glyphs:
    """Unicode only on a real UTF-8 terminal; plain ASCII for pipes and legacy codepages."""
    if os.environ.get("GITHERD_ASCII"):
        return ASCII_GLYPHS
    encoding = console.encoding.lower().replace("_", "-")
    if console.is_terminal and encoding.startswith("utf"):
        return UNICODE_GLYPHS
    return ASCII_GLYPHS


def make_console(
    file: TextIO | None = None,
    *,
    width: int | None = None,
    force_terminal: bool | None = None,
    record: bool = False,
) -> Console:
    return Console(
        file=file, width=width, force_terminal=force_terminal, record=record,
        theme=THEME, highlight=False, emoji=False,
    )
