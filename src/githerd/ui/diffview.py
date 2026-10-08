from __future__ import annotations

import re
from typing import Callable, Sequence

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

from githerd.diffs import FileDiff
from githerd.outcomes import FileChange
from githerd.textsafe import clean_message

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


Loader = Callable[[FileChange], FileDiff]
LOADING_TEXT = "(loading...)"


class ViewerState:
    """Pure scrolling/navigation state so behaviour is testable without a terminal.

    Eager use: ``ViewerState(list_of_FileDiff)``. Lazy use: ``ViewerState(list_of_FileChange,
    loader=fn)``; ``fn`` is called synchronously, once per file, only when that file becomes
    the one shown (``load_current``, which navigation calls), and its result is cached. A
    loader that raises becomes that file's text, so the viewer never crashes on a load.
    """

    def __init__(
        self,
        files: Sequence[FileDiff | FileChange],
        page: int = 20,
        *,
        loader: Loader | None = None,
    ) -> None:
        self.files = files if isinstance(files, list) else list(files)
        self.page = page
        self.index = 0
        self.offset = 0
        self._loader = loader
        self._loaded: dict[int, FileDiff] = {}
        if loader is None:  # eager: every entry already carries its text
            self._loaded = {i: f for i, f in enumerate(self.files) if isinstance(f, FileDiff)}

    def load_current(self) -> None:
        """Load the file being shown if it is not loaded yet (no-op otherwise)."""
        if not self.files or self.index in self._loaded or self._loader is None:
            return
        change = self.files[self.index]
        try:
            self._loaded[self.index] = self._loader(change)
        except Exception as exc:  # a failed load must never take the viewer down
            reason = clean_message(str(exc)) or type(exc).__name__
            self._loaded[self.index] = FileDiff(
                change.path, change.status, f"(could not load {change.path}: {reason})"
            )

    @property
    def text(self) -> str:
        loaded = self._loaded.get(self.index)
        return loaded.text if loaded is not None else LOADING_TEXT

    @property
    def lines(self) -> list[str]:
        """Raw diff lines, split on LF only (so a bare CR or VT never splits a line)."""
        if not self.files:
            return []
        parts = self.text.split("\n")
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
            self.load_current()

    def prev_file(self) -> None:
        if self.index > 0:
            self.index -= 1
            self.offset = 0
            self.load_current()

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
    return _run_viewer(ViewerState(files), input, output)


def run_lazy_diff_viewer(
    changes: list[FileChange],
    loader: Loader,
    *,
    input: Input | None = None,
    output: Output | None = None,
) -> ViewerState:
    """Open the viewer on not-yet-loaded files; ``loader`` runs only for files shown."""
    return _run_viewer(ViewerState(changes, loader=loader), input, output)


def _run_viewer(state: ViewerState, input: Input | None, output: Output | None) -> ViewerState:
    if not state.files:
        return state
    state.load_current()

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
