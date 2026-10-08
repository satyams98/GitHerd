from __future__ import annotations

from dataclasses import dataclass

from rich.console import Group
from rich.text import Text

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Diverged, Failed, FileChange,
    NetworkError, Ok, Outcome, UpToDate, describe,
)
from githerd.textsafe import safe_path
from githerd.ui.theme import Glyphs


@dataclass(frozen=True)
class Action:
    key: str
    id: str
    label: str


SKIP = Action("k", "skip", "skip")


def needs_attention(outcome: Outcome) -> bool:
    return not isinstance(outcome, (Ok, UpToDate))


def actions_for(outcome: Outcome) -> list[Action]:
    if isinstance(outcome, BlockedDirty):
        return [Action("d", "diff", "diff"), Action("s", "stash_pull", "stash & pull"), SKIP]
    if isinstance(outcome, AuthRequired):
        return [Action("a", "auth", "authenticate"), SKIP]
    if isinstance(outcome, (NetworkError, Failed)):
        return [Action("r", "retry", "retry"), SKIP]
    if isinstance(outcome, (Diverged, Conflict)):
        return [SKIP]
    return []


def hint_bar(actions: list[Action], glyphs: Glyphs) -> Text:
    bar = Text()
    for index, action in enumerate(actions):
        if index:
            bar.append(f" {glyphs.sep} ", style="dim")
        bar.append(action.key, style="accent")
        bar.append(f" {action.label}", style="dim")
    return bar


def _status_style(code: str) -> str:
    if "U" in code or code in ("DD", "AA"):
        return "error"
    if "?" in code:
        return "dim"
    if "D" in code:
        return "error"
    if "A" in code:
        return "ok"
    return "warn"


def _heading(name: str, outcome: Outcome, glyphs: Glyphs) -> Text:
    glyph, style = (glyphs.fail, "error") if isinstance(outcome, (NetworkError, Failed)) else (glyphs.attn, "warn")
    line = Text()
    line.append(f"{glyph} ", style=style)
    line.append(safe_path(name), style="subject")
    line.append(f" {glyphs.sep} ", style="dim")
    line.append(describe(outcome), style=style)
    return line


def _file_line(change: FileChange, blocking: set[str]) -> Text:
    line = Text("    ")
    line.append(f"{change.status:<2} ", style=_status_style(change.status.strip() or change.status))
    line.append(safe_path(change.path))
    if change.path in blocking:
        line.append("  (blocks pull)", style="warn")
    return line


def render_card(name: str, outcome: Outcome, glyphs: Glyphs, *, max_files: int = 8) -> Group:
    lines: list[Text] = [_heading(name, outcome, glyphs)]
    if isinstance(outcome, BlockedDirty):
        blocking = set(outcome.blocking)
        ordered = outcome.files
        if len(ordered) > max_files:  # never hide a blocker under "+N more"
            ordered = (
                [c for c in ordered if c.path in blocking]
                + [c for c in ordered if c.path not in blocking]
            )
        shown = ordered[:max_files]
        lines.extend(_file_line(c, blocking) for c in shown)
        hidden = len(outcome.files) - len(shown)
        if hidden > 0:
            lines.append(Text(f"    +{hidden} more", style="dim"))
    elif isinstance(outcome, Conflict):
        lines.extend(Text(f"    {safe_path(path)}", style="error") for path in outcome.files[:max_files])
        hidden = len(outcome.files) - max_files
        if hidden > 0:
            lines.append(Text(f"    +{hidden} more", style="dim"))
    elif isinstance(outcome, AuthRequired) and outcome.remote:
        lines.append(Text(f"    remote: {safe_path(outcome.remote)}", style="dim"))
    return Group(*lines)
