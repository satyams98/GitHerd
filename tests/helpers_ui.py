import io

from rich.console import Console

from githerd.ui.theme import THEME


def render_plain(renderable, width: int = 80) -> str:
    """Render any rich renderable to plain text (no colour, no cursor control)."""
    buf = io.StringIO()
    Console(file=buf, width=width, force_terminal=False, theme=THEME, highlight=False).print(renderable)
    return buf.getvalue()
