import io
import time

import pytest
from rich.cells import cell_len
from rich.console import Console

from githerd import gitops
from githerd.gitops import pull
from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Failed, FileChange, NetworkError, describe,
)
from githerd.repos import GitError, snapshot_all
from githerd.runner import GitResult
from githerd.textsafe import clean_message, safe_path
from githerd.ui.cards import render_card
from githerd.ui.rows import RowState, render_row
from githerd.ui.theme import ASCII_GLYPHS, THEME
from tests.helpers_ui import render_plain

ESC = "\x1b"
DUBIOUS = (
    "fatal: detected dubious ownership in repository at 'C:/w/a'\n"
    "To add an exception for this directory, call:\n"
    "\n\tgit config --global --add safe.directory C:/w/a\n"
)


# ---- clean_message ---------------------------------------------------------------

def test_csi_sequences_are_stripped():
    assert clean_message("a\x1b[2Jb\x1b[31;1mc\x1b[0m") == "abc"


def test_osc_sequences_are_stripped_with_bel_and_st_terminators():
    assert clean_message("a\x1b]0;evil title\x07b") == "ab"
    assert clean_message("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\b") == "alinkb"


def test_unterminated_osc_does_not_swallow_following_lines():
    assert clean_message("a\x1b]0;title\nnext line") == "a next line"


def test_other_escape_pairs_and_lone_escapes_are_stripped():
    assert clean_message("a\x1bcb\x1b(Bc\x1b") == "abc"
    assert ESC not in clean_message("x\x1b")


def test_control_characters_are_dropped():
    assert clean_message("a\x00b\x07c\x08d\x7fe\x85f\x9bg") == "abcdefg"


def test_dangerous_bidi_and_separator_characters_are_dropped():
    nasty = "a\u202ab\u202eb\u2066c\u2069d\u2028e\u2029f"
    assert clean_message(nasty) == "abbcdef"


def test_plain_directional_marks_are_kept_for_legitimate_rtl_text():
    text = "a\u200eb\u200fc\u061cd"
    assert clean_message(text) == text
    assert safe_path(text) == text


def test_whitespace_runs_including_newlines_collapse_to_single_spaces():
    assert clean_message("  a\nb\r\n\tc \x0b d\x0c\n\n e  ") == "a b c d e"


def test_multi_line_git_text_becomes_one_line():
    out = clean_message(DUBIOUS)
    assert "\n" not in out and "\t" not in out
    assert out.startswith("fatal: detected dubious ownership")
    assert out.endswith("safe.directory C:/w/a")


@pytest.mark.parametrize("text, expected", [
    ("https://user:pw@host/r.git", "https://host/r.git"),
    ("https://user:ghp_SECRET@github.com/o/r.git", "https://github.com/o/r.git"),
    ("fatal: unable to access 'https://tok@host/o/r.git/': timed out",
     "fatal: unable to access 'https://host/o/r.git/': timed out"),
    ("ssh://git:pw@host/r and https://a:b@h2/x", "ssh://host/r and https://h2/x"),
    ("https://user:p@ss@host/r.git", "https://host/r.git"),
    ("http://a:b@c@d/", "http://d/"),
    ("http://u:p@[::1]:8080/x", "http://[::1]:8080/x"),
    ("https://tok@host/r.git", "https://host/r.git"),
])
def test_userinfo_in_urls_is_redacted(text, expected):
    out = clean_message(text)
    assert out == expected
    assert "ghp_SECRET" not in out


def test_urls_without_userinfo_and_scp_style_remotes_are_untouched():
    assert clean_message("see https://host/o/r.git") == "see https://host/o/r.git"
    assert clean_message("git@github.com:o/r.git") == "git@github.com:o/r.git"


@pytest.mark.parametrize("text", [
    "https://github.com/o/r@v1",
    "https://registry.npmjs.org/@scope/pkg",
    "https://host/path?x=a@b",
    "https://host/path#frag@x",
    "mail me at a@b.c or see https://host/x",
])
def test_at_signs_outside_the_authority_are_not_userinfo(text):
    assert clean_message(text) == text


def test_ssh_urls_keep_a_bare_username_but_lose_any_password():
    # A bare username such as ``git`` in an ssh URL is not a secret; tokens in
    # http(s)/other URLs are often passed as the "username", so those are redacted.
    assert clean_message("ssh://git@host/x") == "ssh://git@host/x"
    assert clean_message("git+ssh://git@host/x") == "git+ssh://git@host/x"
    assert clean_message("ssh://git:pw@host/x") == "ssh://host/x"
    assert clean_message("SSH://git@host:22/x") == "SSH://git@host:22/x"


IDEMPOTENCE_TABLE = [
    "https://user:p@ss@host/r.git",
    "http://a:b@c@d/",
    "https://github.com/o/r@v1",
    "https://registry.npmjs.org/@scope/pkg",
    "https://host/path?x=a@b",
    "ssh://git@host/x",
    "ssh://git:pw@host/x",
    "http://u:p@[::1]:8080/x",
    "http://x@http://y:z@h",
    "a://b@c://d:e@f",
    "https://u:p@h/x https://v:q@i/y",
    "https://us\x1b[mer:ghp_SECRET@host/r",
    "@@@://@@@",
    "://@",
    "http://@host",
    "http://:@host",
    "git@github.com:o/r.git",
    "x" * 70 + "://u:p@h",
    "1http://u:p@h",
]


@pytest.mark.parametrize("text", IDEMPOTENCE_TABLE)
def test_clean_message_is_idempotent_on_tricky_inputs(text):
    once = clean_message(text)
    assert clean_message(once) == once
    assert "ghp_SECRET" not in once and "p@ss" not in once


@pytest.mark.parametrize("text", [
    "x" * 200_000,
    "a://" + "x" * 200_000,
    "http://" * 30_000,
    "\x1b]" * 200_000,
    " " * 1_000_000,
    "@" * 200_000,
    "://" * 50_000,
    "a://" + "@" * 200_000,
    "a://" + ":@" * 100_000,
    "a://u:p@" * 30_000,
    "a" * 31 + "://" + "b" * 200_000,
], ids=lambda t: f"{t[:12]!r}x{len(t)}")
def test_clean_message_runs_in_linear_time(text):
    start = time.perf_counter()
    clean_message(text)
    assert time.perf_counter() - start < 1.0


def test_userinfo_hidden_behind_an_escape_is_still_redacted():
    assert clean_message("https://us\x1b[mer:ghp_SECRET@host/r") == "https://host/r"


def test_limit_truncates_with_trailing_dots():
    out = clean_message("abcdefghij", limit=8)
    assert out == "abcde..." and len(out) == 8
    assert clean_message("abc", limit=8) == "abc"
    assert clean_message("abcdefgh", limit=8) == "abcdefgh"


def test_clean_message_is_idempotent():
    for text in (DUBIOUS, "a\x1b[2Jb\n\tc", "https://u:p@h/x", "plain", "", "x" * 50):
        once = clean_message(text)
        assert clean_message(once) == once
    limited = clean_message("x" * 50, limit=10)
    assert clean_message(limited, limit=10) == limited


def test_non_ascii_letters_are_unchanged():
    text = "na\u00efve caf\u00e9 \u65e5\u672c\u8a9e \u0434\u0430"
    assert clean_message(text) == text


# ---- safe_path -------------------------------------------------------------------

def test_safe_path_strips_escapes_and_replaces_controls():
    assert ESC not in safe_path("a\x1b[2Jb")
    assert safe_path("a\x1b[2Jb") == "ab"
    assert safe_path("a\x07b\x00c\x7fd") == "a?b?c?d"
    assert safe_path("a\u202eb\u2066c\u2028d") == "a?b?c?d"


def test_safe_path_keeps_whitespace_and_does_not_redact():
    assert safe_path("my dir/  file name.txt") == "my dir/  file name.txt"
    assert safe_path("https://user:pw@host/x") == "https://user:pw@host/x"


def test_safe_path_replaces_tabs_and_newlines_but_not_spaces():
    assert safe_path("a\tb\nc d") == "a?b?c d"


def test_safe_path_leaves_non_ascii_letters_alone():
    assert safe_path("na\u00efve/\u65e5\u672c.txt") == "na\u00efve/\u65e5\u672c.txt"


# ---- integration: outcome boundary -----------------------------------------------

def test_describe_failed_message_is_one_line_without_escapes():
    text = describe(Failed(message="a\nb\x1b[2Jc"))
    assert "\n" not in text and ESC not in text
    assert text == "failed: a bc"


def test_describe_network_error_is_sanitised_and_redacted():
    text = describe(NetworkError(message="line1\nhttps://u:ghp_SECRET@h/r\x1b]0;x\x07"))
    assert "\n" not in text and ESC not in text and "ghp_SECRET" not in text


def test_tail_is_sanitised():
    out = gitops._tail("one\n\x1b[31mtwo\x1b[0m\nhttps://u:pw@h/x\n")
    assert ESC not in out and "pw@" not in out


def test_classify_failure_message_is_sanitised():
    outcome = gitops.classify_failure("boom \x1b[2J here\n")
    assert isinstance(outcome, Failed) and ESC not in outcome.message


def test_card_for_file_path_with_escape_prints_no_escape():
    outcome = BlockedDirty(
        files=[FileChange(status="??", path="evil\x1b[2Jname.txt")],
        blocking=["evil\x1b[2Jname.txt"],
    )
    assert outcome.files[0].path == "evil\x1b[2Jname.txt"  # stored raw
    text = render_plain(render_card("repo", outcome, ASCII_GLYPHS))
    assert ESC not in text and "evilname.txt" in text and "(blocks pull)" in text
    console = Console(file=io.StringIO(), width=80, force_terminal=True, record=True, theme=THEME)
    console.print(render_card("repo", outcome, ASCII_GLYPHS))
    assert ESC not in console.export_text()
    assert "\x1b[2J" not in console.export_text(styles=True)


def test_conflict_and_auth_cards_print_no_escape():
    conflict = render_plain(render_card("r", Conflict(files=["a\x1b[2Jb", "c\x07d"]), ASCII_GLYPHS))
    assert ESC not in conflict and "\x07" not in conflict
    auth = render_plain(render_card("r", AuthRequired(remote="https://h/\x1b[2Jr.git"), ASCII_GLYPHS))
    assert ESC not in auth


def test_dashboard_row_for_multi_line_git_error_is_one_exact_width_line():
    outcome = Failed(message=str(GitError(DUBIOUS)))
    state = RowState(path="/w/a", name="a", branch="main", status="done", outcome=outcome, elapsed=0.4)
    width = 100
    line = render_row(state, ASCII_GLYPHS, name_w=4, branch_w=6, width=width)
    assert "\n" not in line.plain
    assert cell_len(line.plain) == width
    assert "dubious ownership" in line.plain


async def test_snapshot_all_error_text_is_a_single_clean_line(monkeypatch, tmp_path):
    async def fake_run_git(repo, *args, **kwargs):
        return GitResult(code=128, stdout="", stderr=DUBIOUS + "\x1b[2J")

    monkeypatch.setattr("githerd.repos.run_git", fake_run_git)
    (snap,) = await snapshot_all([tmp_path])
    assert "\n" not in snap.error and ESC not in snap.error
    assert "dubious ownership" in snap.error


async def test_pull_failure_from_a_multi_line_git_error_is_one_line(monkeypatch, tmp_path):
    async def failing_snapshot(repo):
        raise GitError("l1\nl2")

    monkeypatch.setattr(gitops, "snapshot", failing_snapshot)
    outcome = await pull(tmp_path)
    assert isinstance(outcome, Failed)
    assert "\n" not in outcome.message and outcome.message == "l1 l2"


async def test_fetch_failure_from_a_multi_line_git_error_is_one_line(monkeypatch, tmp_path):
    async def failing_snapshot(repo):
        raise GitError("l1\nl2\x1b[2J")

    monkeypatch.setattr(gitops, "snapshot", failing_snapshot)
    outcome = await gitops.fetch(tmp_path)
    assert isinstance(outcome, Failed)
    assert "\n" not in outcome.message and ESC not in outcome.message

