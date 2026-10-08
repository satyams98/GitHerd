from __future__ import annotations

import re

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

from githerd.diffs import FileDiff

DIFF_STYLE = Style.from_dict({
    "diff.header": "bold",
    "diff.hunk": "ansicyan",
    "diff.add": "ansigreen",
    "diff.del": "ansired",
    "title": "bold",
    "hint": "ansibrightblack",
})

_HEADER_PREFIXES = ("+++", "---", "diff ", "index ", "new file", "deleted file", "similarity")

# String-type escapes (OSC, DCS, SOS, PM, APC) run until BEL or ST (ESC \); an
# unterminated one runs to the end of the line or up to the next ESC.
_STRING_ESC = re.compile(r"\x1b[\]PX^_][^\x07\x1b]*(?:\x07|\x1b\\|(?=\x1b)|$)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*(?:[@-~]|$)")
# Remaining ESC sequences: intermediates plus a final byte (e.g. ESC c, ESC ( B), or a lone ESC.
_OTHER_ESC = re.compile(r"\x1b[ -/]*[0-~]?")
# C0 (except TAB), DEL, C1, plus line/paragraph separators and bidi overrides/isolates.
_CONTROL = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def sanitize_line(line: str) -> str:
    """Make file-derived text safe to show: no terminal escapes, no control characters."""
    line = _STRING_ESC.sub("", line)
    line = _CSI.sub("", line)
    line = _OTHER_ESC.sub("", line)
    line = line.replace("\r", "").replace("\t", "    ")
    return _CONTROL.sub("?", line)


def style_line(line: str) -> str:
    if line.startswith(_HEADER_PREFIXES):
        return "class:diff.header"
    if line.startswith("@@"):
        return "class:diff.hunk"
    if line.startswith("+"):
        return "class:diff.add"
    if line.startswith("-"):
        return "class:diff.del"
    return ""


class ViewerState:
    """Pure scrolling/navigation state so behaviour is testable without a terminal."""

    def __init__(self, files: list[FileDiff], page: int = 20) -> None:
        self.files = files
        self.page = page
        self.index = 0
        self.offset = 0

    @property
    def lines(self) -> list[str]:
        """Raw diff lines, split on LF only (so a bare CR or VT never splits a line)."""
        if not self.files:
            return []
        parts = self.files[self.index].text.split("\n")
        if parts and parts[-1] == "":
            parts.pop()
        return parts

    def scroll(self, delta: int) -> None:
        self.offset = max(0, min(self.offset + delta, max(0, len(self.lines) - 1)))

    def page_down(self) -> None:
        self.scroll(self.page)

    def page_up(self) -> None:
        self.scroll(-self.page)

    def top(self) -> None:
        self.offset = 0

    def bottom(self) -> None:
        self.offset = max(0, len(self.lines) - 1)

    def next_file(self) -> None:
        if self.index < len(self.files) - 1:
            self.index += 1
            self.offset = 0

    def prev_file(self) -> None:
        if self.index > 0:
            self.index -= 1
            self.offset = 0

    def formatted_body(self, height: int) -> list[tuple[str, str]]:
        window = [sanitize_line(line) for line in self.lines[self.offset : self.offset + height]]
        return [(style_line(line), line + "\n") for line in window]

    def header(self) -> str:
        current = self.files[self.index]
        status = sanitize_line(current.status).strip() or "?"
        return f" {sanitize_line(current.path)}  [{self.index + 1}/{len(self.files)}]  {status}"

    def footer(self) -> str:
        return " j/k scroll  space/b page  n/p file  g/G top/bottom  q quit"


def run_diff_viewer(
    files: list[FileDiff], *, input: Input | None = None, output: Output | None = None
) -> ViewerState:
    state = ViewerState(files)
    if not files:
        return state

    kb = KeyBindings()

    def bind(keys: tuple[str, ...], action, *, eager: bool = False) -> None:
        # One binding per key: kb.add("j", "down") would bind the SEQUENCE j-then-down.
        for key in keys:
            @kb.add(key, eager=eager)
            def _(event) -> None:
                action()

    bind(("j", "down"), lambda: state.scroll(1))
    bind(("k", "up"), lambda: state.scroll(-1))
    bind((" ", "pagedown"), state.page_down)
    bind(("b", "pageup"), state.page_up)
    bind(("n", "right"), state.next_file)
    bind(("p", "left"), state.prev_file)
    bind(("g", "home"), state.top)
    bind(("G", "end"), state.bottom)

    @kb.add("q", eager=True)
    @kb.add("escape", eager=True)
    @kb.add("c-c", eager=True)
    def _quit(event) -> None:
        event.app.exit()

    def body():
        height = max(1, get_app().output.get_size().rows - 2)
        state.page = max(1, height - 1)
        return state.formatted_body(height)

    layout = Layout(HSplit([
        Window(FormattedTextControl(lambda: [("class:title", state.header())]), height=1),
        Window(FormattedTextControl(body), wrap_lines=False),
        Window(FormattedTextControl(lambda: [("class:hint", state.footer())]), height=1),
    ]))
    Application(
        layout=layout, key_bindings=kb, style=DIFF_STYLE, full_screen=True,
        input=input, output=output,
    ).run()
    return state
