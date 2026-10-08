"""Make text that came from git (or a filesystem) safe to put on a terminal.

Git messages and file names are attacker-influenced: they can carry ESC sequences
(cursor movement, window titles, hyperlinks), bidi overrides that reorder what the
user reads, newlines that break one-line layouts, and ``https://user:token@host``
credentials. ``clean_message`` is for prose (one line, secrets removed);
``safe_path`` is for file names (shape preserved, only dangerous characters removed).
"""
from __future__ import annotations

import re

# String-type escapes (OSC, DCS, SOS, PM, APC) run until BEL or ST (ESC \); an
# unterminated one stops at the end of the line, the next ESC or the end of the text.
_STRING_ESC = re.compile(r"\x1b[\]PX^_][^\x07\x1b\n]*(?:\x07|\x1b\\|(?=[\x1b\n])|\Z)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*(?:[@-~]|\Z)")
# Remaining ESC sequences: intermediates plus a final byte (ESC c, ESC ( B), or a lone ESC.
_OTHER_ESC = re.compile(r"\x1b[ -/]*[0-~]?")
# C0, DEL, C1, line/paragraph separators, bidi marks, embeddings, overrides and isolates.
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
_WHITESPACE_CONTROLS = str.maketrans({c: " " for c in "\t\n\v\f\r"})
_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^\s@]*@")
_ELLIPSIS = "..."


def _strip_escapes(text: str) -> str:
    text = _STRING_ESC.sub("", text)
    text = _CSI.sub("", text)
    return _OTHER_ESC.sub("", text)


def clean_message(text: str, limit: int | None = None) -> str:
    """One safe line: no escapes or control characters, single spaces, no URL credentials.

    ``limit`` caps the result at that many characters, ending in ``...`` when cut.
    """
    text = _strip_escapes(text).translate(_WHITESPACE_CONTROLS)
    text = _UNSAFE.sub("", text)
    text = " ".join(text.split())
    text = _USERINFO.sub(r"\1", text)
    if limit is not None and len(text) > limit:
        keep = max(limit - len(_ELLIPSIS), 0)
        text = (text[:keep] + _ELLIPSIS)[:max(limit, 0)]
    return text


def safe_path(text: str) -> str:
    """A file name safe to print: escapes removed, control characters shown as ``?``."""
    return _UNSAFE.sub("?", _strip_escapes(text))
