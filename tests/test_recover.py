import asyncio
import subprocess
import types

import pytest

import githerd.recover
from githerd.outcomes import Conflict, Diverged, Failed, Ok, UpToDate
from githerd.recover import stash_and_pull
from githerd.runner import GitError, GitResult


def assert_work_not_lost(repo, git, expected: dict[str, str]) -> None:
    """Every path's content is in the working tree, or still held by a stash entry."""
    count = len([ln for ln in git(repo, "stash", "list").splitlines() if ln.strip()])
    for path, content in expected.items():
        target = repo / path
        if target.exists() and target.read_text(encoding="utf-8") == content:
            continue
        found = False
        for i in range(count):
            for spec in (f"stash@{{{i}}}:{path}", f"stash@{{{i}}}^3:{path}"):  # tracked, untracked
                try:
                    if git(repo, "show", spec).replace("\r\n", "\n") == content.strip("\n"):
                        found = True
                except Exception:
                    pass
        assert found, f"{path} was lost: not in the working tree and not in git stash"


async def test_clean_repo_just_pulls(make_repo, push_upstream):
    repo = make_repo("a")
    push_upstream(repo, "new.txt")
    outcome = await stash_and_pull(repo)
    assert isinstance(outcome, Ok)


async def test_clean_and_current_repo_is_up_to_date(make_repo):
    assert await stash_and_pull(make_repo("a")) == UpToDate()


async def test_unrelated_local_changes_are_reapplied(make_repo, push_upstream, git):
    repo = make_repo("a")
    (repo / "README.md").write_text("local edit\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("s\n", encoding="utf-8")
    push_upstream(repo, "other.txt")
    outcome = await stash_and_pull(repo)
    assert isinstance(outcome, Ok)
    assert (repo / "README.md").read_text(encoding="utf-8") == "local edit\n"
    assert (repo / "scratch.txt").exists() and (repo / "other.txt").exists()
    assert git(repo, "stash", "list") == ""
    assert_work_not_lost(repo, git, {"README.md": "local edit\n", "scratch.txt": "s\n"})


async def test_existing_user_stash_is_never_popped_when_nothing_to_stash(
    make_repo, push_upstream, git
):
    repo = make_repo("a")
    (repo / "README.md").write_text("old work\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", "user stash")
    push_upstream(repo, "new.txt")
    outcome = await stash_and_pull(repo)
    assert isinstance(outcome, Ok)
    assert "user stash" in git(repo, "stash", "list")
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\n"


async def test_overlapping_edit_reports_conflict_and_keeps_the_stash(make_repo, push_upstream, git):
    repo = make_repo("a")
    (repo / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(repo, "README.md", content="upstream edit\n")
    outcome = await stash_and_pull(repo)
    assert outcome == Conflict(files=["README.md"], stash_kept=True)
    assert git(repo, "stash", "list") != ""
    assert_work_not_lost(repo, git, {"README.md": "local edit\n"})


async def test_untracked_file_clash_fails_but_keeps_the_stash(make_repo, push_upstream, git):
    repo = make_repo("a")
    (repo / "new.txt").write_text("local\n", encoding="utf-8")
    push_upstream(repo, "new.txt", content="upstream\n")
    outcome = await stash_and_pull(repo)
    assert isinstance(outcome, Failed)
    assert "stash" in outcome.message
    assert git(repo, "stash", "list") != ""
    assert_work_not_lost(repo, git, {"new.txt": "local\n"})


async def test_failed_pull_restores_the_working_tree(make_repo, push_upstream, commit_local, git):
    repo = make_repo("a")
    commit_local(repo, "local.txt")
    push_upstream(repo, "up.txt")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    outcome = await stash_and_pull(repo)
    assert outcome == Diverged(ahead=1, behind=1)
    assert (repo / "README.md").read_text(encoding="utf-8") == "dirty\n"
    assert git(repo, "stash", "list") == ""
    assert_work_not_lost(repo, git, {"README.md": "dirty\n"})


async def test_pull_failure_restores_the_working_tree(make_repo, git):
    repo = make_repo("a")
    git(repo, "branch", "--unset-upstream")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("s\n", encoding="utf-8")
    outcome = await stash_and_pull(repo)
    assert isinstance(outcome, Failed)
    assert git(repo, "stash", "list") == ""
    assert_work_not_lost(repo, git, {"README.md": "dirty\n", "scratch.txt": "s\n"})


@pytest.mark.parametrize("error", [RuntimeError("boom"), GitError("git exploded")])
async def test_unexpected_pull_error_returns_failed_and_keeps_work(
    make_repo, git, monkeypatch, error
):
    repo = make_repo("a")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("s\n", encoding="utf-8")

    async def exploding_pull(repo, on_progress=None):
        raise error

    monkeypatch.setattr(githerd.recover, "pull", exploding_pull)
    outcome = await stash_and_pull(repo)
    assert isinstance(outcome, Failed)
    assert_work_not_lost(repo, git, {"README.md": "dirty\n", "scratch.txt": "s\n"})


async def test_cancellation_propagates_and_keeps_work(make_repo, git, monkeypatch):
    repo = make_repo("a")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")

    async def cancelled_pull(repo, on_progress=None):
        raise asyncio.CancelledError

    monkeypatch.setattr(githerd.recover, "pull", cancelled_pull)
    with pytest.raises(asyncio.CancelledError):
        await stash_and_pull(repo)
    assert_work_not_lost(repo, git, {"README.md": "dirty\n"})


async def test_stash_failure_returns_failed_and_does_not_pull(make_repo, git, monkeypatch):
    repo = make_repo("a")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    real_run_git = githerd.recover.run_git
    pulled = []

    async def failing_stash(repo, *args, **kwargs):
        if args[:2] == ("stash", "push"):
            return GitResult(code=1, stdout="", stderr="fatal: could not write stash\n")
        return await real_run_git(repo, *args, **kwargs)

    async def spy_pull(repo, on_progress=None):
        pulled.append(repo)
        return UpToDate()

    monkeypatch.setattr(githerd.recover, "run_git", failing_stash)
    monkeypatch.setattr(githerd.recover, "pull", spy_pull)
    outcome = await stash_and_pull(repo)
    assert isinstance(outcome, Failed)
    assert "stash" in outcome.message
    assert pulled == []
    assert (repo / "README.md").read_text(encoding="utf-8") == "dirty\n"


# ---- Ctrl+C while a stash is pending -----------------------------------------------

async def test_keyboard_interrupt_after_stash_restores_work_and_propagates(make_repo, git, monkeypatch):
    repo = make_repo("a")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("s\n", encoding="utf-8")

    async def interrupted_pull(repo, on_progress=None):
        assert git(repo, "stash", "list") != ""  # the stash exists when Ctrl+C lands
        raise KeyboardInterrupt

    monkeypatch.setattr(githerd.recover, "pull", interrupted_pull)
    with pytest.raises(KeyboardInterrupt):
        await stash_and_pull(repo)
    assert_work_not_lost(repo, git, {"README.md": "dirty\n", "scratch.txt": "s\n"})
    assert git(repo, "stash", "list") == ""  # popped back into the working tree
    assert (repo / "README.md").read_text(encoding="utf-8") == "dirty\n"


async def test_interrupt_pops_only_our_stash_not_an_older_one(make_repo, git, monkeypatch):
    repo = make_repo("a")
    (repo / "README.md").write_text("older\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", "user stash")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")

    async def interrupted_pull(repo, on_progress=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(githerd.recover, "pull", interrupted_pull)
    with pytest.raises(KeyboardInterrupt):
        await stash_and_pull(repo)
    assert (repo / "README.md").read_text(encoding="utf-8") == "dirty\n"
    assert "user stash" in git(repo, "stash", "list")


async def test_cancelled_async_pop_falls_back_to_a_synchronous_pop(make_repo, git, monkeypatch):
    repo = make_repo("a")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    real_run_git = githerd.recover.run_git

    async def cancelling_pop(repo, *args, **kwargs):
        if args[:2] == ("stash", "pop"):
            raise asyncio.CancelledError
        return await real_run_git(repo, *args, **kwargs)

    async def interrupted_pull(repo, on_progress=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(githerd.recover, "run_git", cancelling_pop)
    monkeypatch.setattr(githerd.recover, "pull", interrupted_pull)
    with pytest.raises(KeyboardInterrupt):
        await stash_and_pull(repo)
    assert_work_not_lost(repo, git, {"README.md": "dirty\n"})
    assert git(repo, "stash", "list") == ""


async def test_failing_emergency_pop_still_propagates_and_keeps_the_stash(make_repo, git, monkeypatch):
    repo = make_repo("a")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    real_run_git = githerd.recover.run_git

    async def broken_pop(repo, *args, **kwargs):
        if args[:2] == ("stash", "pop"):
            raise OSError("cannot spawn git")
        return await real_run_git(repo, *args, **kwargs)

    async def interrupted_pull(repo, on_progress=None):
        raise KeyboardInterrupt

    def broken_sync(*args, **kwargs):
        raise OSError("cannot spawn git")

    monkeypatch.setattr(githerd.recover, "run_git", broken_pop)
    monkeypatch.setattr(githerd.recover, "pull", interrupted_pull)
    monkeypatch.setattr(githerd.recover, "_sync_git", broken_sync)
    with pytest.raises(KeyboardInterrupt):
        await stash_and_pull(repo)
    assert_work_not_lost(repo, git, {"README.md": "dirty\n"})  # still in `git stash list`


def test_sync_git_decodes_as_utf8_and_never_raises_on_undecodable_bytes(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"\x81\x8d ok".decode("utf-8", "replace"), stderr="")

    monkeypatch.setattr(githerd.recover, "subprocess", types.SimpleNamespace(run=fake_run))
    githerd.recover._sync_git(tmp_path, "status")
    assert seen["text"] is True
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"


async def test_undecodable_git_output_does_not_break_the_synchronous_restore(make_repo, git, monkeypatch):
    repo = make_repo("a")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    real_run_git = githerd.recover.run_git
    real_run = subprocess.run

    async def broken_pop(repo, *args, **kwargs):
        if args[:2] == ("stash", "pop"):
            raise asyncio.CancelledError
        return await real_run_git(repo, *args, **kwargs)

    async def interrupted_pull(repo, on_progress=None):
        raise KeyboardInterrupt

    def run_with_cp1252_default(cmd, **kwargs):
        if kwargs.get("encoding") is None and kwargs.get("text"):
            raise UnicodeDecodeError("cp1252", b"\x81", 0, 1, "character maps to <undefined>")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(githerd.recover, "run_git", broken_pop)
    monkeypatch.setattr(githerd.recover, "pull", interrupted_pull)
    shim = types.SimpleNamespace(run=run_with_cp1252_default)  # leave the real module alone
    monkeypatch.setattr(githerd.recover, "subprocess", shim)
    with pytest.raises(KeyboardInterrupt):
        await stash_and_pull(repo)
    assert_work_not_lost(repo, git, {"README.md": "dirty\n"})
    assert git(repo, "stash", "list") == ""  # the synchronous pop really ran


async def test_failed_synchronous_restore_logs_a_warning_without_a_traceback(
    make_repo, git, monkeypatch, caplog
):
    repo = make_repo("a")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    real_run_git = githerd.recover.run_git

    async def broken_pop(repo, *args, **kwargs):
        if args[:2] == ("stash", "pop"):
            raise OSError("cannot spawn git")
        return await real_run_git(repo, *args, **kwargs)

    async def interrupted_pull(repo, on_progress=None):
        raise KeyboardInterrupt

    def broken_sync(*args, **kwargs):
        raise OSError("cannot spawn git")

    monkeypatch.setattr(githerd.recover, "run_git", broken_pop)
    monkeypatch.setattr(githerd.recover, "pull", interrupted_pull)
    monkeypatch.setattr(githerd.recover, "_sync_git", broken_sync)
    with caplog.at_level("DEBUG", logger="githerd.recover"):
        with pytest.raises(KeyboardInterrupt):
            await stash_and_pull(repo)
    records = [r for r in caplog.records if r.name == "githerd.recover"]
    assert records and all(r.levelname == "WARNING" for r in records)
    assert all(r.exc_info is None for r in records)
    assert "cannot spawn git" in caplog.text


# ---- H4 item 6: a conflicted pop says the stash was kept -----------------------------------

async def test_a_conflicted_pop_is_flagged_as_keeping_the_stash(make_repo, push_upstream, git):
    repo = make_repo("a")
    (repo / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(repo, "README.md", content="upstream edit\n")
    outcome = await stash_and_pull(repo)
    assert isinstance(outcome, Conflict) and outcome.stash_kept is True
    assert "auto-stash" in git(repo, "stash", "list")  # git really did keep it


async def test_a_clean_pop_does_not_produce_a_stash_flag(make_repo, push_upstream):
    repo = make_repo("a")
    (repo / "scratch.txt").write_text("s\n", encoding="utf-8")
    push_upstream(repo, "other.txt")
    assert isinstance(await stash_and_pull(repo), Ok)


# ---- H7 item A: is our auto-stash still around? ------------------------------------------------

async def test_auto_stash_present_sees_only_our_own_stash(make_repo, git, tmp_path):
    from githerd.recover import STASH_MESSAGE, auto_stash_present

    repo = make_repo("a")
    assert await auto_stash_present(repo) is False
    (repo / "README.md").write_text("mine\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", "somebody else's stash")
    assert await auto_stash_present(repo) is False
    (repo / "README.md").write_text("ours\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", STASH_MESSAGE)
    assert await auto_stash_present(repo) is True


async def test_auto_stash_present_never_raises(monkeypatch, tmp_path):
    from githerd.recover import auto_stash_present

    assert await auto_stash_present(tmp_path / "missing") is False

    async def boom(*args, **kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(githerd.recover, "run_git", boom)
    assert await auto_stash_present(tmp_path) is False


# ---- H9: an exact "was a NEW stash left behind?" check ---------------------------------------------

async def test_stash_tip_is_empty_without_a_stash_and_the_top_entry_with_one(make_repo, git):
    from githerd.recover import stash_tip

    repo = make_repo("a")
    assert await stash_tip(repo) == ""
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", "first")
    first = await stash_tip(repo)
    assert first == git(repo, "rev-parse", "refs/stash")
    (repo / "README.md").write_text("two\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", "second")
    assert await stash_tip(repo) not in ("", first)


async def test_auto_stash_present_with_a_before_tip_only_counts_a_stash_made_since(make_repo, git):
    from githerd.recover import STASH_MESSAGE, auto_stash_present, stash_tip

    repo = make_repo("a")
    (repo / "README.md").write_text("older\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", STASH_MESSAGE)  # stranded long ago
    before = await stash_tip(repo)
    assert await auto_stash_present(repo) is True  # without a baseline any such stash counts
    assert await auto_stash_present(repo, before) is False  # but nothing new was made
    (repo / "README.md").write_text("new\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", STASH_MESSAGE)
    assert await auto_stash_present(repo, before) is True


async def test_auto_stash_present_with_a_before_tip_ignores_a_new_stash_that_is_not_ours(make_repo, git):
    from githerd.recover import auto_stash_present, stash_tip

    repo = make_repo("a")
    before = await stash_tip(repo)  # no stash at all yet: ""
    (repo / "README.md").write_text("mine\n", encoding="utf-8")
    git(repo, "stash", "push", "-m", "somebody else's stash")
    assert await auto_stash_present(repo, before) is False


async def test_auto_stash_present_with_a_before_tip_never_raises(monkeypatch, tmp_path):
    from githerd.recover import auto_stash_present

    assert await auto_stash_present(tmp_path / "missing", "abc") is False

    async def boom(*args, **kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(githerd.recover, "run_git", boom)
    assert await auto_stash_present(tmp_path, "abc") is False
