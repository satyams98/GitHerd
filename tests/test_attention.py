import asyncio
import io

import pytest

from githerd import attention, recover
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
    assert final[repo] == Conflict(files=["README.md"], stash_kept=True)
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


# ---- H4 item 1: feedback before a slow action ------------------------------------------------

def _fast(monkeypatch, name, outcome):
    async def fake(repo, on_progress=None):
        return outcome

    monkeypatch.setattr(attention, name, fake)


def test_retry_prints_a_pulling_line_before_the_outcome(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()
    _fast(monkeypatch, "pull", UpToDate())
    resolve_attention(console, tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS, read_key=keys("r"))
    lines = console.file.getvalue().splitlines()
    assert lines.index("  pulling a...") < lines.index("  already up to date")


def test_stash_and_pull_prints_its_own_feedback_line(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()
    _fast(monkeypatch, "stash_and_pull", UpToDate())
    resolve_attention(console, tmp_path, {repo: BlockedDirty(files=[])}, ASCII_GLYPHS, read_key=keys("s"))
    lines = console.file.getvalue().splitlines()
    assert lines.index("  stashing, pulling, restoring a...") < lines.index("  already up to date")


def test_authenticate_prints_a_credentials_warning_before_handing_off(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()
    seen = []
    monkeypatch.setattr(
        attention, "run_git_interactive", lambda r, *a: seen.append(console.file.getvalue()) or 0,
    )
    _fast(monkeypatch, "pull", UpToDate())
    resolve_attention(
        console, tmp_path, {repo: AuthRequired(remote="origin")}, ASCII_GLYPHS, read_key=keys("a"),
    )
    line = "  running git fetch for a (git may ask for credentials)..."
    assert line in seen[0]  # already on screen when git takes over the terminal
    assert console.file.getvalue().splitlines().index(line) < console.file.getvalue().splitlines().index(
        "  already up to date"
    )


def test_the_feedback_label_is_sanitised(tmp_path, monkeypatch):
    repo = tmp_path / "evil\u202e\x1b[2Jname"  # never touches the file system
    console = make()
    _fast(monkeypatch, "pull", UpToDate())
    resolve_attention(console, tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS, read_key=keys("r"))
    out = console.file.getvalue()
    assert "  pulling evil?name..." in out
    assert "\x1b" not in out and "\u202e" not in out


# ---- H4 item 2: actions honour the pull timeout ----------------------------------------------

async def _hangs(repo, on_progress=None):
    await asyncio.sleep(30)
    return UpToDate()


def test_a_slow_retry_times_out_as_failed(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    monkeypatch.setattr(attention, "pull", _hangs)
    final = resolve_attention(
        make(), tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS,
        read_key=keys("r", "k"), timeout=0.2,
    )
    assert final[repo] == Failed(message="timed out after 0.2s")


def test_a_slow_stash_and_pull_times_out_as_failed(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    monkeypatch.setattr(attention, "stash_and_pull", _hangs)
    console = make()
    final = resolve_attention(
        console, tmp_path, {repo: BlockedDirty(files=[])}, ASCII_GLYPHS,
        read_key=keys("s", "k"), timeout=0.2,
    )
    assert final[repo] == Failed(message="timed out after 0.2s")
    assert "failed: timed out after 0.2s" in console.file.getvalue()


def test_a_slow_pull_after_authenticating_times_out(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    monkeypatch.setattr(attention, "run_git_interactive", lambda r, *a: 0)
    monkeypatch.setattr(attention, "pull", _hangs)
    final = resolve_attention(
        make(), tmp_path, {repo: AuthRequired(remote="o")}, ASCII_GLYPHS,
        read_key=keys("a", "k"), timeout=0.2,
    )
    assert final[repo] == Failed(message="timed out after 0.2s")


@pytest.mark.parametrize("timeout", [None, 0])
def test_no_timeout_means_the_action_is_not_limited(make_repo, monkeypatch, tmp_path, timeout):
    repo = make_repo("a")

    async def slowish(repo, on_progress=None):
        await asyncio.sleep(0.3)
        return UpToDate()

    monkeypatch.setattr(attention, "pull", slowish)
    final = resolve_attention(
        make(), tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS,
        read_key=keys("r"), timeout=timeout,
    )
    assert final[repo] == UpToDate()


def test_the_interactive_authenticate_hand_off_is_not_timed(make_repo, monkeypatch, tmp_path):
    import time

    repo = make_repo("a")
    monkeypatch.setattr(attention, "run_git_interactive", lambda r, *a: time.sleep(0.5) or 0)
    _fast(monkeypatch, "pull", UpToDate())
    final = resolve_attention(
        make(), tmp_path, {repo: AuthRequired(remote="o")}, ASCII_GLYPHS,
        read_key=keys("a"), timeout=0.1,  # the user is typing: only the pull after it is limited
    )
    assert final[repo] == UpToDate()


def test_a_head_move_before_the_timeout_is_still_journaled(make_repo, push_upstream, git, monkeypatch, tmp_path):
    repo = make_repo("a")
    push_upstream(repo, "new.txt")
    git(repo, "fetch")
    root = tmp_path / "work"

    async def moves_then_hangs(path, on_progress=None):
        git(path, "reset", "--hard", "origin/main")  # HEAD moves, then the pull never finishes
        await asyncio.sleep(30)
        return UpToDate()

    monkeypatch.setattr(attention, "pull", moves_then_hangs)
    final = resolve_attention(
        make(), root, {repo: Failed(message="x")}, ASCII_GLYPHS, read_key=keys("r", "k"), timeout=0.5,
    )
    assert final[repo] == Failed(message="timed out after 0.5s")
    (entry,) = Journal(root).last_undoable().entries
    assert entry.repo == str(repo) and entry.after_head != entry.before_head and entry.branch == "main"


# ---- H4 item 3: a non-retryable failure offers no retry ---------------------------------------

def test_a_non_retryable_failure_shows_no_retry_key(make_repo, tmp_path):
    repo = make_repo("a")
    console = make()
    outcome = Failed(message="detached HEAD: switch to a branch before pulling", retryable=False)
    final = resolve_attention(console, tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("r", "k"))
    out = console.file.getvalue()
    assert "retry" not in out and "k skip" in out
    assert final[repo] == outcome  # the stray `r` did nothing


# ---- H4 item 7: skip all ------------------------------------------------------------------------

def _three(make_repo, outcome=None):
    repos = [make_repo(n) for n in ("a", "b", "c")]
    return repos, {r: outcome or Failed(message=f"boom {r.name}") for r in repos}


def _no_pull(monkeypatch):
    async def forbidden(repo, on_progress=None):
        raise AssertionError("nothing may be pulled after skip all")

    monkeypatch.setattr(attention, "pull", forbidden)


def test_skip_all_on_the_first_card_touches_nothing_and_reads_no_more_keys(make_repo, monkeypatch, tmp_path):
    repos, results = _three(make_repo)
    _no_pull(monkeypatch)
    console = make()
    final = resolve_attention(console, tmp_path, results, ASCII_GLYPHS, read_key=keys("x"))
    assert final == results
    out = console.file.getvalue()
    assert "boom a" in out and "boom b" not in out and "boom c" not in out  # no further cards


def test_skip_then_skip_all_leaves_the_second_and_third_alone(make_repo, monkeypatch, tmp_path):
    repos, results = _three(make_repo)
    _no_pull(monkeypatch)
    console = make()
    final = resolve_attention(console, tmp_path, results, ASCII_GLYPHS, read_key=keys("k", "x"))
    assert final == results
    out = console.file.getvalue()
    assert "boom b" in out and "boom c" not in out


def test_skip_all_keeps_the_outcome_a_failed_retry_left_behind(make_repo, monkeypatch, tmp_path):
    repos, results = _three(make_repo)
    calls = []

    async def still_failing(repo, on_progress=None):
        calls.append(repo)
        return Failed(message="still failing")

    monkeypatch.setattr(attention, "pull", still_failing)
    final = resolve_attention(make(), tmp_path, results, ASCII_GLYPHS, read_key=keys("r", "x"))
    assert calls == [repos[0]]
    assert final[repos[0]] == Failed(message="still failing")
    assert final[repos[1]] == results[repos[1]] and final[repos[2]] == results[repos[2]]


def test_skip_all_is_offered_only_while_more_repos_need_attention(make_repo, tmp_path):
    repos, results = _three(make_repo)
    console = make()
    resolve_attention(console, tmp_path, results, ASCII_GLYPHS, read_key=keys("k", "k", "k"))
    out = console.file.getvalue()
    assert out.count("x skip all") == 2  # not on the last card
    last_card = out[out.index("boom c"):]
    assert "x skip all" not in last_card


def test_a_single_repo_never_offers_skip_all(make_repo, tmp_path):
    repo = make_repo("a")
    console = make()
    resolve_attention(console, tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS, read_key=keys("k"))
    assert "skip all" not in console.file.getvalue()


def test_repos_that_need_nothing_do_not_count_as_remaining(make_repo, tmp_path):
    needy, fine = make_repo("a"), make_repo("b")
    console = make()
    resolve_attention(
        console, tmp_path, {needy: Failed(message="x"), fine: UpToDate()}, ASCII_GLYPHS, read_key=keys("k"),
    )
    assert "skip all" not in console.file.getvalue()


def test_skip_all_still_journals_work_already_done(make_repo, push_upstream, tmp_path):
    first, second, third = make_repo("a"), make_repo("b"), make_repo("c")
    push_upstream(first, "new.txt")
    root = tmp_path / "work"
    results = {first: Failed(message="1"), second: Failed(message="2"), third: Failed(message="3")}
    final = resolve_attention(make(), root, results, ASCII_GLYPHS, read_key=keys("r", "x"))
    assert isinstance(final[first], Ok)
    assert final[second] == results[second] and final[third] == results[third]
    (entry,) = Journal(root).last_undoable().entries
    assert entry.repo == str(first)


# ---- H7 item A: a timeout must not leave the user's work in `git stash` unannounced -------------

KEPT_SUFFIX = " (your local changes are still in 'git stash')"


def test_a_timeout_that_strands_the_stash_says_so(make_repo, push_upstream, git, monkeypatch, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    monkeypatch.setattr(recover, "pull", _hangs)  # stash push works, then the pull never finishes

    async def restore_fails(repo, before):  # the best-effort pop after the cancel does not happen
        return None

    monkeypatch.setattr(recover, "_restore_after_interrupt", restore_fails)
    console = make()
    final = resolve_attention(
        console, tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("s", "k"), timeout=2,
    )
    assert final[repo] == Failed(message="timed out after 2s" + KEPT_SUFFIX)
    out = console.file.getvalue()
    assert STASH_NOTE in out
    assert "failed: timed out after 2s" + KEPT_SUFFIX in out
    assert recover.STASH_MESSAGE in git(repo, "stash", "list")  # the work really is in the stash


def test_a_timeout_whose_restore_succeeds_prints_no_stash_note(make_repo, push_upstream, git, monkeypatch, tmp_path):
    repo, outcome = _blocked(make_repo, push_upstream)
    monkeypatch.setattr(recover, "pull", _hangs)
    console = make()
    final = resolve_attention(
        console, tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("s", "k"), timeout=2,
    )
    assert final[repo] == Failed(message="timed out after 2s")
    assert STASH_NOTE not in console.file.getvalue()
    assert git(repo, "stash", "list") == ""
    assert (repo / "README.md").read_text(encoding="utf-8") == "local edit\n"


def test_a_plain_retry_timeout_never_looks_at_the_stash(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")

    async def no_look(repo):
        raise AssertionError("a plain pull cannot have stashed anything")

    monkeypatch.setattr(attention, "auto_stash_present", no_look)
    monkeypatch.setattr(attention, "pull", _hangs)
    final = resolve_attention(
        make(), tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS, read_key=keys("r", "k"), timeout=0.2,
    )
    assert final[repo] == Failed(message="timed out after 0.2s")


# ---- H7 item B: resolve_attention validates its timeout like bulk ---------------------------

@pytest.mark.parametrize("timeout", [-1, -0.5, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("needs_attention", [True, False])
def test_an_invalid_timeout_is_rejected_up_front(make_repo, tmp_path, timeout, needs_attention):
    repo = make_repo("a")

    def no_keys():
        raise AssertionError("must fail before any key is read")

    outcome = Failed(message="x") if needs_attention else UpToDate()
    with pytest.raises(ValueError, match="timeout must be a positive number of seconds"):
        resolve_attention(
            make(), tmp_path / "root", {repo: outcome}, ASCII_GLYPHS, read_key=no_keys, timeout=timeout,
        )
    assert not (tmp_path / "root").exists()  # nothing was journaled either


def test_validate_timeout_is_public_and_none_or_zero_mean_no_timeout():
    from githerd.bulk import validate_timeout

    assert validate_timeout(None) is None
    assert validate_timeout(0) is None
    assert validate_timeout(2.5) == 2.5
    with pytest.raises(ValueError):
        validate_timeout(-1)


# ---- H7 item H: the lazy viewer loads from inside prompt_toolkit's running loop -----------------

def test_two_files_walked_with_n_then_q_load_each_once_inside_the_running_loop(
    make_repo, push_upstream, monkeypatch, tmp_path
):
    import threading

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from githerd.ui.diffview import run_lazy_diff_viewer

    repo = make_repo("a")
    for name in ("x.txt", "y.txt"):  # untracked locally, added upstream: both block the pull
        (repo / name).write_text(f"local {name}\n", encoding="utf-8")
        push_upstream(repo, name, content=f"upstream {name}\n")
    outcome = asyncio.run(pull(repo))
    assert isinstance(outcome, BlockedDirty) and len(outcome.files) == 2

    loaded = []
    real = attention.file_diff

    async def spy(r, change):
        loaded.append(change.path)  # runs in the worker's loop, under the viewer's running loop
        return await real(r, change)

    monkeypatch.setattr(attention, "file_diff", spy)
    states, errors = [], []
    console = make()
    with create_pipe_input() as inp:
        inp.send_text("nq")

        def viewer(changes, loader):
            try:
                states.append(run_lazy_diff_viewer(changes, loader, input=inp, output=DummyOutput()))
            except Exception as exc:  # the attention loop would hide it as a dim note
                errors.append(exc)
                raise

        worker = threading.Thread(target=lambda: resolve_attention(
            console, tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("d", "k"), viewer=viewer,
        ), daemon=True)
        worker.start()
        worker.join(60)
        assert not worker.is_alive(), "the viewer did not exit"
    assert errors == []
    assert "diff viewer unavailable" not in console.file.getvalue()
    assert sorted(loaded) == ["x.txt", "y.txt"]  # each file exactly once
    (state,) = states
    assert "upstream" not in state.text and "local" in state.text


# ---- H5 item 2: the feedback lines quietly show the real git command ----------------------------

STASH_PULL_CMD = "    $ git stash push --include-untracked ; git pull --ff-only ; git stash pop"
RETRY_CMD = "    $ git pull --ff-only"
AUTH_CMD = "    $ git fetch"


def test_the_command_constants_live_next_to_the_code_that_runs_them():
    assert recover.STASH_PULL_COMMAND == STASH_PULL_CMD.strip()[2:]
    from githerd import gitops

    assert gitops.PULL_FF_COMMAND == RETRY_CMD.strip()[2:]
    assert gitops.FETCH_COMMAND == AUTH_CMD.strip()[2:]


def test_retry_shows_its_command_under_the_pulling_line(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()
    _fast(monkeypatch, "pull", UpToDate())
    resolve_attention(console, tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS, read_key=keys("r"))
    lines = console.file.getvalue().splitlines()
    assert lines[lines.index("  pulling a...") + 1] == RETRY_CMD


def test_stash_and_pull_shows_its_command_under_the_feedback_line(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()
    _fast(monkeypatch, "stash_and_pull", UpToDate())
    resolve_attention(console, tmp_path, {repo: BlockedDirty(files=[])}, ASCII_GLYPHS, read_key=keys("s"))
    lines = console.file.getvalue().splitlines()
    assert lines[lines.index("  stashing, pulling, restoring a...") + 1] == STASH_PULL_CMD


def test_authenticate_shows_its_command_before_handing_off(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()
    seen = []
    monkeypatch.setattr(
        attention, "run_git_interactive", lambda r, *a: seen.append(console.file.getvalue()) or 0,
    )
    _fast(monkeypatch, "pull", UpToDate())
    resolve_attention(
        console, tmp_path, {repo: AuthRequired(remote="origin")}, ASCII_GLYPHS, read_key=keys("a"),
    )
    lines = seen[0].splitlines()
    assert lines[lines.index("  running git fetch for a (git may ask for credentials)...") + 1] == AUTH_CMD


def test_the_command_lines_are_dim_ascii_and_one_line_each(make_repo, monkeypatch, tmp_path):
    from rich.console import Console

    from githerd.ui.theme import THEME

    repo = make_repo("a")
    console = Console(
        file=io.StringIO(), width=120, force_terminal=True, color_system="standard", record=True,
        theme=THEME, highlight=False,
    )
    _fast(monkeypatch, "stash_and_pull", UpToDate())
    resolve_attention(console, tmp_path, {repo: BlockedDirty(files=[])}, ASCII_GLYPHS, read_key=keys("s"))
    styled = console.export_text(styles=True)
    assert "\x1b[2m" + STASH_PULL_CMD in styled
    assert STASH_PULL_CMD.isascii()


# ---- H6 item 6: labels for duplicate repo names ---------------------------------------------------------

def test_labels_name_the_repo_in_the_card_and_in_the_feedback(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()
    _fast(monkeypatch, "pull", UpToDate())
    resolve_attention(
        console, tmp_path, {repo: Failed(message="x")}, ASCII_GLYPHS,
        read_key=keys("r"), labels={repo: "team-a/a"},
    )
    out = console.file.getvalue()
    assert "x team-a/a | failed: x" in out  # the card heading
    assert "  pulling team-a/a..." in out
    assert "  pulling a..." not in out


def test_labels_are_used_for_the_stash_and_auth_feedback_too(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    console = make()
    monkeypatch.setattr(attention, "run_git_interactive", lambda r, *a: 0)
    _fast(monkeypatch, "stash_and_pull", UpToDate())
    _fast(monkeypatch, "pull", UpToDate())
    labels = {repo: "team-a/a"}
    resolve_attention(
        console, tmp_path, {repo: BlockedDirty(files=[])}, ASCII_GLYPHS, read_key=keys("s"), labels=labels,
    )
    resolve_attention(
        console, tmp_path, {repo: AuthRequired(remote="origin")}, ASCII_GLYPHS, read_key=keys("a"), labels=labels,
    )
    out = console.file.getvalue()
    assert "  stashing, pulling, restoring team-a/a..." in out
    assert "  running git fetch for team-a/a (git may ask for credentials)..." in out


def test_a_label_is_sanitised_and_missing_labels_fall_back_to_the_name(make_repo, monkeypatch, tmp_path):
    repo = make_repo("a")
    other = make_repo("b")
    console = make()
    _fast(monkeypatch, "pull", UpToDate())
    resolve_attention(
        console, tmp_path, {repo: Failed(message="x"), other: Failed(message="y")}, ASCII_GLYPHS,
        read_key=keys("r", "r"), labels={repo: f"evil{chr(0x202E)}\x1b[2Jname"},
    )
    out = console.file.getvalue()
    assert "  pulling evil?name..." in out and "  pulling b..." in out
    assert "\x1b" not in out and chr(0x202E) not in out


# ---- H9: the timeout note is exact -----------------------------------------------------------------------

def _older_githerd_stash(repo, git):
    (repo / "README.md").write_text("an older stranded change\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", recover.STASH_MESSAGE)
    assert recover.STASH_MESSAGE in git(repo, "stash", "list")


def _blocked_with_older_stash(make_repo, push_upstream, git):
    repo = make_repo("a")
    _older_githerd_stash(repo, git)
    (repo / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(repo, "README.md", content="upstream edit\n")
    outcome = asyncio.run(pull(repo))
    assert isinstance(outcome, BlockedDirty)
    return repo, outcome


def test_an_older_stranded_stash_does_not_trigger_the_note_when_ours_was_restored(
    make_repo, push_upstream, git, monkeypatch, tmp_path
):
    repo, outcome = _blocked_with_older_stash(make_repo, push_upstream, git)
    monkeypatch.setattr(recover, "pull", _hangs)  # stash push works, then the pull never finishes
    console = make()
    final = resolve_attention(
        console, tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("s", "k"), timeout=2,
    )
    assert final[repo] == Failed(message="timed out after 2s")  # no stash suffix
    assert STASH_NOTE not in console.file.getvalue()
    assert (repo / "README.md").read_text(encoding="utf-8") == "local edit\n"  # ours was put back
    assert len(git(repo, "stash", "list").splitlines()) == 1  # only the older one is left


def test_a_new_stash_left_behind_still_triggers_the_note_when_an_older_one_exists(
    make_repo, push_upstream, git, monkeypatch, tmp_path
):
    repo, outcome = _blocked_with_older_stash(make_repo, push_upstream, git)
    monkeypatch.setattr(recover, "pull", _hangs)

    async def restore_fails(repo, before):
        return None

    monkeypatch.setattr(recover, "_restore_after_interrupt", restore_fails)
    console = make()
    final = resolve_attention(
        console, tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("s", "k"), timeout=2,
    )
    assert final[repo] == Failed(message="timed out after 2s" + KEPT_SUFFIX)
    assert STASH_NOTE in console.file.getvalue()
    assert len(git(repo, "stash", "list").splitlines()) == 2


def test_an_unreadable_stash_tip_before_the_action_falls_back_to_the_old_check(
    make_repo, push_upstream, git, monkeypatch, tmp_path
):
    repo, outcome = _blocked_with_older_stash(make_repo, push_upstream, git)
    monkeypatch.setattr(recover, "pull", _hangs)

    async def no_tip(repo):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(attention, "stash_tip", no_tip)
    console = make()
    resolve_attention(
        console, tmp_path, {repo: outcome}, ASCII_GLYPHS, read_key=keys("s", "k"), timeout=2,
    )
    assert STASH_NOTE in console.file.getvalue()  # unsure, so err on the side of telling the user
