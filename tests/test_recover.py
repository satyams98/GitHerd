import asyncio

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
    assert outcome == Conflict(files=["README.md"])
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
