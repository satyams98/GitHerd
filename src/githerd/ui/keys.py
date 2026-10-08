from __future__ import annotations

import sys
from typing import Callable

from rich.console import Console

from githerd.ui.cards import Action, hint_bar
from githerd.ui.theme import Glyphs

KeyReader = Callable[[], str]


def read_key() -> str:
    """Read one key press from the console without waiting for Enter."""
    if sys.platform == "win32":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # arrow/function keys send a two-part code
            msvcrt.getwch()
            return ""
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    return sys.stdin.read(1)  # non-Windows fallback; githerd targets Windows


def choose_action(
    console: Console, actions: list[Action], glyphs: Glyphs, read_key: KeyReader = read_key
) -> Action:
    skip = next((a for a in actions if a.id == "skip"), None)
    console.print(hint_bar(actions, glyphs))
    while True:
        try:
            key = read_key()
        except (EOFError, OSError):
            if skip is not None:
                return skip
            raise
        if key == "\x1b" and skip is not None:
            return skip
        for action in actions:
            if key.lower() == action.key:
                return action
