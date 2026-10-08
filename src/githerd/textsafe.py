"""Make text that came from git (or a filesystem) safe to put on a terminal.

Git messages and file names are attacker-influenced: they can carry ESC sequences
(cursor movement, window titles, hyperlinks), bidi overrides that reorder what the
user reads, newlines that break one-line layouts, and ``https://user:token@host``
credentials. ``clean_message`` is for prose (one line, secrets removed);
``safe_path`` is for file names (shape preserved, only dangerous characters removed).

Notes on behaviour:

* ``clean_message`` collapses runs of whitespace, so two spaces inside a file name that
  appears in a message (e.g. a ``Failed`` text) become one. That is accepted: messages
  are prose, and ``safe_path`` is the function that keeps names exact.
* The plain directional marks U+200E, U+200F and U+061C are kept, because legitimate
  right-to-left text uses them. The embedding, override and isolate controls
  (U+202A-U+202E, U+2066-U+2069) are removed (``safe_path`` shows them as ``?``).
* URL credentials are removed from the authority only (the part between ``://`` and the
  first ``/``, ``?``, ``#`` or whitespace), taking everything up to the LAST ``@`` there,
  so ``https://user:p@ss@host/x`` becomes ``https://host/x`` while ``@`` in a path or
  query (``/o/r@v1``, ``/@scope/pkg``, ``?x=a@b``) is left alone. A bare user name with
  no ``:`` password is kept for ssh-family schemes (``ssh://git@host/x``: ``git`` is a
  convention, not a secret) and removed for every other scheme, because a token is
  often passed as the "user name" (``https://ghp_token@github.com/o/r``).
"""
from __future__ import annotations

import re

# String-type escapes (OSC, DCS, SOS, PM, APC) run until BEL or ST (ESC \); an
# unterminated one stops at the end of the line, the next ESC or the end of the text.
_STRING_ESC = re.compile(r"\x1b[\]PX^_][^\x07\x1b\n]*(?:\x07|\x1b\\|(?=[\x1b\n])|\Z)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*(?:[@-~]|\Z)")
# Remaining ESC sequences: intermediates plus a final byte (ESC c, ESC ( B), or a lone ESC.
_OTHER_ESC = re.compile(r"\x1b[ -/]*[0-~]?")
# C0, DEL, C1, line/paragraph separators, bidi embeddings, overrides and isolates. The plain
# marks U+200E/U+200F/U+061C are legitimate in right-to-left text and are not stripped.
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
_WHITESPACE_CONTROLS = str.maketrans({c: " " for c in "\t\n\v\f\r"})
# scheme "://" userinfo "@": the scheme is at most 32 characters (no lookbehind, so a URL glued
# to preceding text is still found; at most 32 attempts per position), and the userinfo stays
# inside the authority (no "/", "?", "#" or whitespace), so the search is linear in the text.
_USERINFO = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,31})://(?P<userinfo>[^\s/?#]*)@"
)
_KEEP_BARE_USER = frozenset({"ssh", "git+ssh", "ssh+git"})
_MAX_REDACT_PASSES = 4
_ELLIPSIS = "..."


def _strip_escapes(text: str) -> str:
    text = _STRING_ESC.sub("", text)
    text = _CSI.sub("", text)
    return _OTHER_ESC.sub("", text)


def _redact_userinfo_match(match: re.Match[str]) -> str:
    scheme, userinfo = match.group("scheme"), match.group("userinfo")
    if ":" not in userinfo and scheme.lower() in _KEEP_BARE_USER:
        return match.group(0)  # a bare ssh user name is not a secret
    return f"{scheme}://"


def _redact_userinfo(text: str) -> str:
    for _ in range(_MAX_REDACT_PASSES):  # removal can, rarely, expose another URL
        redacted = _USERINFO.sub(_redact_userinfo_match, text)
        if redacted == text:
            break
        text = redacted
    return text


def clean_message(text: str, limit: int | None = None) -> str:
    """One safe line: no escapes or control characters, single spaces, no URL credentials.

    ``limit`` caps the result at that many characters, ending in ``...`` when cut.
    """
    text = _strip_escapes(text).translate(_WHITESPACE_CONTROLS)
    text = _UNSAFE.sub("", text)
    text = " ".join(text.split())
    text = _redact_userinfo(text)
    if limit is not None and len(text) > limit:
        keep = max(limit - len(_ELLIPSIS), 0)
        text = (text[:keep] + _ELLIPSIS)[:max(limit, 0)]
    return text


def safe_path(text: str) -> str:
    """A file name safe to print: escapes removed, control characters shown as ``?``."""
    return _UNSAFE.sub("?", _strip_escapes(text))
