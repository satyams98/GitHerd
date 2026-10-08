from __future__ import annotations

import sys
from typing import Callable

from rich.console import Console

from githerd.ui.cards import Action, hint_bar
from githerd.ui.theme import Glyphs

KeyReader = Callable[[], str]

MAX_CONSECUTIVE_EMPTY = 50
SPECIAL_KEY = "\x00"  # what read_key returns for an arrow/function key; "" means "nothing came"


def read_key() -> str:
    """Read one key press from the console without waiting for Enter.

    Special keys (arrows, function keys) are swallowed and reported as ``SPECIAL_KEY``,
    which is never empty: an empty string means only that nothing came (a dead console).
    """
    if sys.platform == "win32":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # arrow/function keys send a two-part code
            msvcrt.getwch()
            return SPECIAL_KEY
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    # Non-Windows fallback; githerd targets Windows. Here "" means stdin hit EOF.
    ch = sys.stdin.read(1)
    if ch == "":
        raise EOFError
    return ch


def flush_input() -> None:
    """Discard keys typed while something else was running (Windows console only).

    Without this, a key pressed during a slow action would answer the NEXT card.
    """
    if sys.platform != "win32":
        return
    try:
        import msvcrt

        while msvcrt.kbhit():
            msvcrt.getwch()
    except OSError:  # no console to drain: nothing to flush
        pass


def choose_action(
    console: Console, actions: list[Action], glyphs: Glyphs,
    read_key: KeyReader = read_key, flush: Callable[[], None] = flush_input,
) -> Action:
    """Show the hint bar and block until the user presses a key bound to an action.

    Typed-ahead keys are discarded with ``flush`` (called once, after the hint bar is
    printed and before the first read; an exception from it is ignored). Keys match
    case-insensitively on both sides. Escape selects the ``skip`` action when one is
    offered. The ``SPECIAL_KEY`` sentinel (an arrow key) is ignored and counts as a real
    key. If the reader raises EOFError/OSError, or returns an empty string
    ``MAX_CONSECUTIVE_EMPTY`` times in a row (a dead console), ``skip`` is returned when
    offered; otherwise the error propagates (EOFError for the dead reader case),
    because there is no safe default action to choose.

    Raises ValueError for an empty action list or duplicate action keys.
    """
    if not actions:
        raise ValueError("no actions to choose from")
    seen: set[str] = set()
    for action in actions:
        folded = action.key.lower()
        if folded in seen:
            raise ValueError(f"duplicate action key: {action.key!r}")
        seen.add(folded)

    skip = next((a for a in actions if a.id == "skip"), None)
    console.print(hint_bar(actions, glyphs))
    try:
        flush()
    except Exception:  # flushing is a courtesy; never let it block the choice
        pass
    empties = 0
    while True:
        try:
            key = read_key()
            if key == "":
                empties += 1
                if empties >= MAX_CONSECUTIVE_EMPTY:
                    raise EOFError("key reader returned nothing; console is dead")
            else:
                empties = 0
        except (EOFError, OSError):
            if skip is not None:
                return skip
            raise
        if key == SPECIAL_KEY:  # an arrow key: ignored, and proof the console is alive
            continue
        if key == "\x1b" and skip is not None:
            return skip
        for action in actions:
            if key.lower() == action.key.lower():
                return action
