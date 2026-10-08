import asyncio
import io

import pytest

from githerd import attention
from githerd.attention import exit_code_for, resolve_attention
from githerd.gitops import pull
from githerd.journal import Journal
from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Failed, Ok, UpToDate,
)
from githerd.ui.theme import ASCII_GLYPHS, make_console


def keys(*seq):
    it = iter(seq)
    return lambda: next(it)


def make():
    console = make_console(io.StringIO(), width=100, force_terminal=False)
    return console


def test_exit_code_policy():
    ok = Ok(commits=1, files=1, before_head="a", after_head="b")
    assert exit_code_for([ok, UpToDate()]) == 0
    assert exit_code_for([]) == 0
    assert exit_code_for([ok, Failed(message="x")]) == 2
    assert exit_code_for([AuthRequired(remote="o")]) == 2
    assert exit_code_for([Conflict(files=["a"])]) == 2


def test_nothing_to_resolve_never_reads_a_key(tmp_path):
    def no_keys():
        raise AssertionError("no key should be read")

    results = {tmp_path / "a": UpToDate()}
    assert resolve_attention(make(), tmp_path, results, ASCII_GLYPHS, read_key=no_keys) == results


def _blocked(make_repo, push_upstream):
    repo = make_repo("a")
    (repo / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(repo, "README.md", content="upstream edit\n")
    outcome = asyncio.run(pull(repo))
    assert isinstance(outcome, BlockedDirty)
    return repo, outcome


def test_diff_then_skip_opens_the_viewer_once(make_repo, push_upstream, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    opened = []

    def fake_viewer(changes, loader):  # opens by calling the loader for every change
        opened.append([loader(c).path for c in changes])

    final = resolve_attention(
        make(), tmp_path, {repo: outcome}, ASCII_GLYPHS,
        read_key=keys("d", "k"), viewer=fake_viewer,
    )
    assert opened == [["README.md"]]
    assert final[repo] == outcome


def test_stash_and_pull_conflict_is_shown_then_skipped(make_repo, push_upstream, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    console = make()
    final = resolve_attention(
        console, tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("s", "k"),
    )
    assert final[repo] == Conflict(files=["README.md"])
    out = console.file.getvalue()
    assert "conflict in 1 file" in out


def test_stash_and_pull_conflict_still_journals_the_moved_head(make_repo, push_upstream, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    root = tmp_path / "work"
    final = resolve_attention(
        make(), root, {repo: outcome}, ASCII_GLYPHS, read_key=keys("s", "k"),
    )
    assert isinstance(final[repo], Conflict)  # no outcome carries heads, yet HEAD moved
    op_set = Journal(root).last_undoable()
    assert op_set is not None and op_set.description == "resolve 1 repos"
    (entry,) = op_set.entries
    assert entry.repo == str(repo) and entry.op == "pull"
    assert entry.after_head != entry.before_head


def test_retry_resolves_and_journals(make_repo, push_upstream, tmp_path):
    repo = make_repo("a")
    push_upstream(repo, "new.txt")
    root = tmp_path / "work"
    final = resolve_attention(
        make(), root, {repo: Failed(message="transient")}, ASCII_GLYPHS, read_key=keys("r"),
    )
    assert isinstance(final[repo], Ok)
    op_set = Journal(root).last_undoable()
    assert op_set is not None and op_set.description == "resolve 1 repos"
    assert [e.repo for e in op_set.entries] == [str(repo)]


def test_unmoved_head_is_not_journalled(make_repo, tmp_path):
    repo = make_repo("a")
    root = tmp_path / "work"
    final = resolve_attention(
        make(), root, {repo: Failed(message="transient")}, ASCII_GLYPHS, read_key=keys("r"),
    )
    assert final[repo] == UpToDate()
    assert Journal(root).last_undoable() is None


def test_authenticate_hands_off_to_interactive_git_then_retries(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    calls = []
    monkeypatch.setattr(attention, "run_git_interactive", lambda r, *a: calls.append((r, a)) or 0)
    final = resolve_attention(
        make(), tmp_path, {repo: AuthRequired(remote="origin")}, ASCII_GLYPHS, read_key=keys("a"),
    )
    assert calls == [(repo, ("fetch",))]
    assert final[repo] == UpToDate()


def test_default_key_reader_is_looked_up_at_call_time(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    monkeypatch.setattr("githerd.ui.keys.read_key", keys("k"))
    final = resolve_attention(make(), tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS)
    assert final[repo] == Failed(message="x")


def test_a_dead_diff_viewer_does_not_crash_the_loop(make_repo, push_upstream, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    console = make()

    def broken_viewer(changes, loader):
        raise EOFError("no console")

    final = resolve_attention(
        console, tmp_path, {repo: outcome}, ASCII_GLYPHS,
        read_key=keys("d", "k"), viewer=broken_viewer,
    )
    assert final[repo] == outcome
    assert "diff viewer unavailable: no console" in console.file.getvalue()


def test_ctrl_c_stops_the_loop_but_journals_work_already_done(make_repo, push_upstream, tmp_path):
    first = make_repo("a")
    second = make_repo("b")
    push_upstream(first, "new.txt")
    root = tmp_path / "work"
    it = iter(["r"])

    def read_key():
        try:
            return next(it)
        except StopIteration:
            raise KeyboardInterrupt from None

    results = {first: Failed(message="one"), second: Failed(message="two")}
    with pytest.raises(KeyboardInterrupt):
        resolve_attention(make(), root, results, ASCII_GLYPHS, read_key=read_key)
    op_set = Journal(root).last_undoable()
    assert op_set is not None
    assert [e.repo for e in op_set.entries] == [str(first)]


def test_results_are_not_mutated_and_every_repo_is_returned(make_repo, tmp_path):
    needy = make_repo("a")
    fine = make_repo("b")
    results = {needy: Failed(message="x"), fine: UpToDate()}
    snapshot = dict(results)
    final = resolve_attention(
        make(), tmp_path, results, ASCII_GLYPHS, read_key=keys("r"),
    )
    assert results == snapshot
    assert final is not results
    assert set(final) == {needy, fine}
    assert final[needy] == UpToDate()
    assert final[fine] == UpToDate()


def test_outcome_text_is_printed_literally_and_output_is_ascii(make_repo, tmp_path):
    repo = make_repo("a")
    console = make()
    final = resolve_attention(
        console, tmp_path, {repo: Failed(message="[bold]boom[/bold]")}, ASCII_GLYPHS,
        read_key=keys("k"),
    )
    assert final[repo] == Failed(message="[bold]boom[/bold]")
    out = console.file.getvalue()
    assert "[bold]boom[/bold]" in out
    assert out.isascii()


# ---- review fixes: viewer failures and stranded stashes ----------------------------

STASH_NOTE = "if you had local changes, check 'git stash list'"


def test_any_diff_viewer_failure_is_a_dim_note_not_a_crash(make_repo, push_upstream, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    console = make()

    def broken_viewer(changes, loader):
        raise RuntimeError("NoConsoleScreenBufferError: no console")

    final = resolve_attention(
        console, tmp_path, {repo: outcome}, ASCII_GLYPHS,
        read_key=keys("d", "k"), viewer=broken_viewer,
    )
    assert final[repo] == outcome
    out = console.file.getvalue()
    assert "diff viewer unavailable: NoConsoleScreenBufferError: no console" in out
    assert out.isascii()


def test_ctrl_c_during_stash_and_pull_prints_the_stash_note(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()

    async def interrupted(repo, on_progress=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(attention, "stash_and_pull", interrupted)
    with pytest.raises(KeyboardInterrupt):
        resolve_attention(
            console, tmp_path, {repo: BlockedDirty(files=[])}, ASCII_GLYPHS, read_key=keys("s"),
        )
    out = console.file.getvalue()
    assert STASH_NOTE in out
    assert out.isascii()


def test_ctrl_c_during_a_plain_retry_prints_no_stash_note(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()

    async def interrupted(repo, on_progress=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(attention, "pull", interrupted)
    with pytest.raises(KeyboardInterrupt):
        resolve_attention(
            console, tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS, read_key=keys("r"),
        )
    assert STASH_NOTE not in console.file.getvalue()


# ---- H2 item 3: entries carry the branch ---------------------------------------------

def test_retry_journals_the_branch_it_pulled(make_repo, push_upstream, tmp_path):
    repo = make_repo("a")
    push_upstream(repo, "new.txt")
    root = tmp_path / "work"
    resolve_attention(
        make(), root, {repo: Failed(message="transient")}, ASCII_GLYPHS, read_key=keys("r"),
    )
    (entry,) = Journal(root).last_undoable().entries
    assert entry.branch == "main"


def test_stash_and_pull_conflict_journals_the_branch(make_repo, push_upstream, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    root = tmp_path / "work"
    resolve_attention(make(), root, {repo: outcome}, ASCII_GLYPHS, read_key=keys("s", "k"))
    (entry,) = Journal(root).last_undoable().entries
    assert entry.after_head != entry.before_head
    assert entry.branch == "main"


def test_a_detached_head_is_journaled_without_a_branch(make_repo, push_upstream, git, monkeypatch, tmp_path):
    repo = make_repo("a")
    push_upstream(repo, "new.txt")
    git(repo, "fetch")
    git(repo, "checkout", "--detach")
    root = tmp_path / "work"

    async def moves_detached_head(path, on_progress=None):
        git(path, "reset", "--hard", "origin/main")  # HEAD moves while detached
        return UpToDate()

    monkeypatch.setattr(attention, "pull", moves_detached_head)
    resolve_attention(
        make(), root, {repo: Failed(message="x")}, ASCII_GLYPHS, read_key=keys("r"),
    )
    (entry,) = Journal(root).last_undoable().entries
    assert entry.branch is None


# ---- H3 item 4: the viewer loads diffs lazily ------------------------------------------

def test_the_viewer_gets_changes_and_a_sync_loader_that_has_not_run_yet(
    make_repo, push_upstream, monkeypatch, tmp_path
):
    repo, outcome = _blocked(make_repo, push_upstream)
    spawned = []
    real = attention.file_diff

    async def spy(r, change):
        spawned.append(change.path)
        return await real(r, change)

    monkeypatch.setattr(attention, "file_diff", spy)
    seen = {}

    def viewer(changes, loader):
        seen["changes"] = [c.path for c in changes]
        seen["before"] = list(spawned)
        seen["diff"] = loader(changes[0])

    resolve_attention(
        make(), tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("d", "k"), viewer=viewer,
    )
    assert seen["changes"] == ["README.md"]
    assert seen["before"] == []  # opening the viewer spawned no git at all
    assert spawned == ["README.md"]
    assert "+local edit" in seen["diff"].text


def test_the_loader_works_inside_a_running_event_loop(make_repo, push_upstream, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    texts = []

    def viewer(changes, loader):
        async def inside_a_loop():  # like a prompt_toolkit key handler: asyncio.run would raise
            texts.append(loader(changes[0]).text)

        asyncio.run(inside_a_loop())

    final = resolve_attention(
        make(), tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("d", "k"), viewer=viewer,
    )
    assert final[repo] == outcome
    assert "+local edit" in texts[0]


def test_the_default_viewer_is_the_lazy_one(make_repo, push_upstream, monkeypatch, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    calls = []

    def fake_lazy(changes, loader, **kwargs):
        calls.append(([c.path for c in changes], loader(changes[0]).path))

    monkeypatch.setattr(attention, "run_lazy_diff_viewer", fake_lazy)
    resolve_attention(make(), tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("d", "k"))
    assert calls == [(["README.md"], "README.md")]


def test_the_real_lazy_viewer_loads_through_the_sync_loader(make_repo, push_upstream, tmp_path):
    import threading

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from githerd.ui.diffview import run_lazy_diff_viewer

    repo, outcome = _blocked(make_repo, push_upstream)
    states = []
    with create_pipe_input() as inp:
        inp.send_text("q")

        def viewer(changes, loader):
            states.append(run_lazy_diff_viewer(changes, loader, input=inp, output=DummyOutput()))

        worker = threading.Thread(target=lambda: resolve_attention(
            make(), tmp_path, {repo: outcome}, ASCII_GLYPHS,
            read_key=keys("d", "k"), viewer=viewer,
        ), daemon=True)
        worker.start()
        worker.join(30)
        assert not worker.is_alive(), "the viewer did not exit"
    (state,) = states
    assert "+local edit" in state.text
