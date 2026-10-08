from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.text import Text

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Diverged, Ok, Outcome, UpToDate, describe,
)
from githerd.repos import RepoSnapshot
from githerd.ui.theme import Glyphs

Status = Literal["queued", "running", "done"]


@dataclass
class RowState:
    path: str
    name: str
    branch: str = ""
    status: Status = "queued"
    phase: str = ""
    percent: int | None = None
    outcome: Outcome | None = None
    elapsed: float | None = None
    started: float | None = None


def bar(percent: int, width: int, glyphs: Glyphs) -> str:
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return glyphs.bar_on * filled + glyphs.bar_off * (width - filled)


def fit(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def _glyph_and_style(state: RowState, glyphs: Glyphs) -> tuple[str, str]:
    if state.status == "queued":
        return glyphs.queued, "dim"
    if state.status == "running":
        return glyphs.running, "accent"
    outcome = state.outcome
    if isinstance(outcome, Ok):
        return glyphs.ok, "ok"
    if isinstance(outcome, UpToDate):
        return glyphs.ok, "dim"
    if isinstance(outcome, (BlockedDirty, Diverged, Conflict, AuthRequired)):
        return glyphs.attn, "warn"
    return glyphs.fail, "error"


def _detail(state: RowState, glyphs: Glyphs, bar_w: int) -> str:
    if state.status == "queued":
        return "queued"
    if state.status == "running":
        if state.percent is None:
            return state.phase or f"working{glyphs.ellipsis}"
        return f"{fit(state.phase, 18):<18} {bar(state.percent, bar_w, glyphs)} {state.percent:>3}%"
    return describe(state.outcome) if state.outcome is not None else ""


def render_row(
    state: RowState, glyphs: Glyphs, *, name_w: int, branch_w: int, width: int, bar_w: int = 10
) -> Text:
    glyph, style = _glyph_and_style(state, glyphs)
    line = Text()
    line.append(f"{glyph} ", style=style)
    line.append(fit(state.name, name_w).ljust(name_w), style="subject")
    line.append("  " + fit(state.branch, branch_w).ljust(branch_w), style="dim")
    line.append("  ")
    detail_style = "dim" if state.status == "queued" or isinstance(state.outcome, UpToDate) else ""
    line.append(_detail(state, glyphs, bar_w), style=detail_style)
    suffix = f"{state.elapsed:.1f}s" if state.elapsed is not None else ""
    room = width - len(suffix) - 1 if suffix else width
    line.truncate(room, overflow="ellipsis", pad=True)
    if suffix:
        line.append(" " + suffix, style="dim")
    return line


def render_status(snaps: list[RepoSnapshot], glyphs: Glyphs) -> Text:
    name_w = min(24, max((len(s.name) for s in snaps), default=0))
    branch_w = min(16, max((len(s.branch or "(detached)") for s in snaps), default=0))
    out = Text()
    for index, snap in enumerate(snaps):
        if index:
            out.append("\n")
        if snap.error:
            first = snap.error.splitlines()[0] if snap.error.strip() else snap.error
            out.append(f"{glyphs.fail} ", style="error")
            out.append(fit(snap.name, name_w).ljust(name_w), style="subject")
            out.append(f"  error: {first}", style="error")
            continue
        glyph, style = (glyphs.attn, "warn") if snap.dirty else (glyphs.ok, "ok")
        branch = snap.branch or "(detached)"
        sync = f"{glyphs.ahead}{snap.ahead} {glyphs.behind}{snap.behind}" if snap.upstream else "no upstream"
        changes = f"{len(snap.dirty)} changed" if snap.dirty else "clean"
        out.append(f"{glyph} ", style=style)
        out.append(fit(snap.name, name_w).ljust(name_w), style="subject")
        out.append("  " + fit(branch, branch_w).ljust(branch_w), style="dim")
        out.append("  " + sync.ljust(11), style="dim")
        out.append(changes, style=style if snap.dirty else "dim")
    return out
