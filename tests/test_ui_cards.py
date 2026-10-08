from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Diverged, Failed, FileChange,
    NetworkError, Ok, UpToDate,
)
from githerd.ui.cards import SKIP, Action, actions_for, hint_bar, needs_attention, render_card
from githerd.ui.theme import ASCII_GLYPHS, UNICODE_GLYPHS
from tests.helpers_ui import render_plain

OK = Ok(commits=1, files=1, before_head="a", after_head="b")


def changes(*specs):
    return [FileChange(status=s, path=p) for s, p in specs]


def test_needs_attention_only_for_problems():
    assert not needs_attention(OK)
    assert not needs_attention(UpToDate())
    for outcome in (
        BlockedDirty(files=[]), Diverged(ahead=1, behind=1), Conflict(files=["a"]),
        AuthRequired(remote="origin"), NetworkError(message="x"), Failed(message="x"),
    ):
        assert needs_attention(outcome)


def test_actions_per_outcome():
    ids = lambda o: [a.id for a in actions_for(o)]  # noqa: E731
    assert ids(BlockedDirty(files=[])) == ["diff", "stash_pull", "skip"]
    assert ids(AuthRequired(remote="o")) == ["auth", "skip"]
    assert ids(NetworkError(message="x")) == ["retry", "skip"]
    assert ids(Failed(message="x")) == ["retry", "skip"]
    assert ids(Diverged(ahead=1, behind=1)) == ["skip"]
    assert ids(Conflict(files=["a"])) == ["skip"]
    assert ids(OK) == [] and ids(UpToDate()) == []


def test_hint_bar_uses_the_glyph_separator():
    actions = [Action("d", "diff", "diff"), Action("s", "stash_pull", "stash & pull"), SKIP]
    assert hint_bar(actions, UNICODE_GLYPHS).plain == "d diff · s stash & pull · k skip"
    assert hint_bar(actions, ASCII_GLYPHS).plain == "d diff | s stash & pull | k skip"


def test_blocked_dirty_card_lists_files_and_marks_blockers():
    outcome = BlockedDirty(
        files=changes((" M", "src/config.yaml"), ("??", "scratch.txt")),
        blocking=["src/config.yaml"],
    )
    text = render_plain(render_card("infra", outcome, ASCII_GLYPHS))
    assert "! infra" in text and "blocked: 2 local changes" in text
    assert " M src/config.yaml" in text and "(blocks pull)" in text
    assert "?? scratch.txt" in text
    assert text.count("(blocks pull)") == 1


def test_blocked_dirty_card_collapses_long_file_lists():
    files = changes(*[(" M", f"f{i}.txt") for i in range(12)])
    text = render_plain(render_card("infra", BlockedDirty(files=files), ASCII_GLYPHS, max_files=8))
    assert "f7.txt" in text and "f8.txt" not in text
    assert "+4 more" in text


def test_other_cards_show_the_relevant_facts():
    diverged = render_plain(render_card("api", Diverged(ahead=1, behind=2), ASCII_GLYPHS))
    assert "diverged: 1 ahead, 2 behind" in diverged
    conflict = render_plain(render_card("api", Conflict(files=["a.txt", "b.txt"]), ASCII_GLYPHS))
    assert "conflict in 2 files" in conflict and "a.txt" in conflict and "b.txt" in conflict
    failed = render_plain(render_card("api", Failed(message="boom"), ASCII_GLYPHS))
    assert "x api" in failed and "boom" in failed
    auth = render_plain(render_card("api", AuthRequired(remote="https://host/o/r.git"), ASCII_GLYPHS))
    assert "needs credentials" in auth and "https://host/o/r.git" in auth
    net = render_plain(render_card("api", NetworkError(message="timed out"), ASCII_GLYPHS))
    assert "network error: timed out" in net


def test_blocking_files_are_shown_first_when_the_list_is_truncated():
    files = changes(*[(" M", f"f{i}.txt") for i in range(12)])
    outcome = BlockedDirty(files=files, blocking=["f10.txt", "f11.txt"])
    text = render_plain(render_card("infra", outcome, ASCII_GLYPHS, max_files=8))
    assert "f10.txt" in text and "f11.txt" in text
    assert text.count("(blocks pull)") == 2
    assert "+4 more" in text
    assert "f5.txt" in text and "f6.txt" not in text  # the rest keep their order
    lines = [ln for ln in text.splitlines() if ".txt" in ln]
    assert [ln.split()[1] for ln in lines] == [
        "f10.txt", "f11.txt", "f0.txt", "f1.txt", "f2.txt", "f3.txt", "f4.txt", "f5.txt",
    ]


def test_card_order_is_unchanged_when_nothing_is_truncated():
    files = changes((" M", "b.txt"), ("??", "a.txt"), (" M", "c.txt"))
    for blocking in (["c.txt"], []):
        text = render_plain(render_card("r", BlockedDirty(files=files, blocking=blocking), ASCII_GLYPHS))
        assert [ln.split()[1] for ln in text.splitlines() if ".txt" in ln] == ["b.txt", "a.txt", "c.txt"]


def test_conflict_card_collapses_long_file_lists_with_a_more_line():
    outcome = Conflict(files=[f"c{i}.txt" for i in range(11)])
    text = render_plain(render_card("api", outcome, ASCII_GLYPHS, max_files=8))
    assert "c7.txt" in text and "c8.txt" not in text
    assert "+3 more" in text
    short = render_plain(render_card("api", Conflict(files=["a.txt", "b.txt"]), ASCII_GLYPHS))
    assert "more" not in short
