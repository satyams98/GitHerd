from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.cells import cell_len, set_cell_size
from rich.text import Text

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Diverged, Failed, NetworkError, Ok, Outcome, UpToDate,
    describe,
)
from githerd.repos import RepoSnapshot
from githerd.textsafe import clean_message
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
    filled = int(width * percent / 100)
    return glyphs.bar_on * filled + glyphs.bar_off * (width - filled)


def fit(text: str, width: int, glyphs: Glyphs) -> str:
    """Fit ``text`` into ``width`` terminal cells, marking truncation with the glyph set's ellipsis."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    marker = glyphs.ellipsis
    marker_w = cell_len(marker)
    if width <= marker_w:
        return set_cell_size(marker, width)
    return set_cell_size(text, width - marker_w) + marker


def _pad(text: str, width: int) -> str:
    """Pad with spaces to ``width`` cells (cell-aware replacement for ``str.ljust``)."""
    return text + " " * max(0, width - cell_len(text))


def _glyph_and_style(state: RowState, glyphs: Glyphs) -> tuple[str, str]:
    if state.status == "queued":
        return glyphs.queued, "dim"
    if state.status == "running":
        return glyphs.running, "accent"
    outcome = state.outcome
    if outcome is None:
        return glyphs.attn, "warn"
    if isinstance(outcome, Ok):
        return glyphs.ok, "ok"
    if isinstance(outcome, UpToDate):
        return glyphs.ok, "dim"
    if isinstance(outcome, (BlockedDirty, Diverged, Conflict, AuthRequired)):
        return glyphs.attn, "warn"
    if isinstance(outcome, (NetworkError, Failed)):
        return glyphs.fail, "error"
    return glyphs.fail, "error"


def _detail(state: RowState, glyphs: Glyphs, bar_w: int) -> str:
    if state.status == "queued":
        return "queued"
    if state.status == "running":
        if state.percent is None:
            return state.phase or f"working{glyphs.ellipsis}"
        return f"{_pad(fit(state.phase, 18, glyphs), 18)} {bar(state.percent, bar_w, glyphs)} {state.percent:>3}%"
    return describe(state.outcome) if state.outcome is not None else "no result"


def render_row(
    state: RowState, glyphs: Glyphs, *, name_w: int, branch_w: int, width: int, bar_w: int = 10
) -> Text:
    glyph, style = _glyph_and_style(state, glyphs)
    line = Text()
    line.append(f"{glyph} ", style=style)
    line.append(_pad(fit(state.name, name_w, glyphs), name_w), style="subject")
    line.append("  " + _pad(fit(state.branch, branch_w, glyphs), branch_w), style="dim")
    line.append("  ")
    detail_style = "dim" if state.status == "queued" or isinstance(state.outcome, UpToDate) else ""
    line.append(_detail(state, glyphs, bar_w), style=detail_style)
    suffix = f"{state.elapsed:.1f}s" if state.elapsed is not None else ""
    room = max(width, 0)
    if suffix:
        suffix_room = width - cell_len(suffix) - 1
        if suffix_room >= 1:
            room = suffix_room
        else:
            suffix = ""
    marker = glyphs.ellipsis
    if cell_len(line.plain) > room:
        marker_w = cell_len(marker)
        if room <= marker_w:
            line.truncate(0, overflow="crop")
            line.append(set_cell_size(marker, room))
        else:
            line.truncate(room - marker_w, overflow="crop")
            line.append(marker)
    shortfall = room - cell_len(line.plain)
    if shortfall > 0:
        line.append(" " * shortfall)
    if suffix:
        line.append(" " + suffix, style="dim")
    return line


def _sync_text(snap: RepoSnapshot, glyphs: Glyphs) -> str:
    if not snap.upstream:
        return "no upstream"
    return f"{glyphs.ahead}{snap.ahead} {glyphs.behind}{snap.behind}"


def render_status(
    snaps: list[RepoSnapshot], glyphs: Glyphs, *, max_name_width: int | None = 24
) -> Text:
    """One line per repo. ``max_name_width=None`` never truncates names (plain, piped output)."""
    longest_name = max((cell_len(s.name) for s in snaps), default=0)
    name_w = longest_name if max_name_width is None else min(max_name_width, longest_name)
    branch_w = min(16, max((cell_len(s.branch or "(detached)") for s in snaps), default=0))
    sync_w = max((cell_len(_sync_text(s, glyphs)) for s in snaps if not s.error), default=0)
    out = Text()
    for index, snap in enumerate(snaps):
        if index:
            out.append("\n")
        if snap.error:
            first = clean_message(snap.error.splitlines()[0]) if snap.error.strip() else snap.error
            out.append(f"{glyphs.fail} ", style="error")
            out.append(_pad(fit(snap.name, name_w, glyphs), name_w), style="subject")
            out.append(f"  error: {first}", style="error")
            continue
        glyph, style = (glyphs.attn, "warn") if snap.dirty else (glyphs.ok, "ok")
        branch = snap.branch or "(detached)"
        changes = f"{len(snap.dirty)} changed" if snap.dirty else "clean"
        out.append(f"{glyph} ", style=style)
        out.append(_pad(fit(snap.name, name_w, glyphs), name_w), style="subject")
        out.append("  " + _pad(fit(branch, branch_w, glyphs), branch_w), style="dim")
        out.append("  " + _pad(_sync_text(snap, glyphs), sync_w), style="dim")
        out.append("  ")  # columns are always separated, whatever the sync text width
        out.append(changes, style=style if snap.dirty else "dim")
    return out
