import asyncio

import pytest

from githerd import diffs
from githerd.diffs import MAX_PARALLEL_DIFFS, diffs_for, file_diff
from githerd.outcomes import FileChange
from githerd.repos import snapshot
from githerd.runner import GitResult


async def _change(repo, path):
    snap = await snapshot(repo)
    return next(c for c in snap.dirty if c.path == path)


async def test_modified_file(make_repo):
    repo = make_repo("a")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    result = await file_diff(repo, await _change(repo, "README.md"))
    assert result.path == "README.md"
    assert "-hello" in result.text and "+changed" in result.text


async def test_untracked_file(make_repo):
    repo = make_repo("a")
    (repo / "new.txt").write_text("brand new\n", encoding="utf-8")
    result = await file_diff(repo, await _change(repo, "new.txt"))
    assert "+brand new" in result.text


async def test_staged_new_file(make_repo, git):
    repo = make_repo("a")
    (repo / "staged.txt").write_text("s\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    result = await file_diff(repo, await _change(repo, "staged.txt"))
    assert "+s" in result.text


async def test_deleted_file(make_repo):
    repo = make_repo("a")
    (repo / "README.md").unlink()
    result = await file_diff(repo, await _change(repo, "README.md"))
    assert "-hello" in result.text


async def test_large_diff_is_truncated(make_repo, monkeypatch):
    monkeypatch.setattr(diffs, "MAX_DIFF_CHARS", 50)
    repo = make_repo("a")
    (repo / "README.md").write_text("x\n" * 200, encoding="utf-8")
    result = await file_diff(repo, await _change(repo, "README.md"))
    assert result.text.endswith("(truncated)")
    assert len(result.text) < 120


async def test_empty_diff_explains_itself(make_repo):
    repo = make_repo("a")
    result = await file_diff(repo, FileChange(status=" M", path="README.md"))  # unchanged file
    assert "no textual changes" in result.text


async def test_diffs_for_preserves_order(make_repo):
    repo = make_repo("a")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "z.txt").write_text("z\n", encoding="utf-8")
    snap = await snapshot(repo)
    results = await diffs_for(repo, snap.dirty)
    assert [r.path for r in results] == [c.path for c in snap.dirty]


async def test_non_ascii_and_binary_untracked_never_raise(make_repo):
    repo = make_repo("a")
    (repo / "naïve-日本.txt").write_text("héllo\n", encoding="utf-8")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00\xff" * 100)
    snap = await snapshot(repo)
    names = {c.path for c in snap.dirty}
    assert {"naïve-日本.txt", "blob.bin"} <= names
    results = {r.path: r for r in await diffs_for(repo, snap.dirty)}
    text = results["naïve-日本.txt"].text
    assert "+héllo" in text
    # core.quotePath=false: the header carries the raw name, not octal escapes
    assert "naïve-日本.txt" in text
    assert "\\303" not in text
    assert "Binary files" in results["blob.bin"].text


async def test_leading_dash_filename_untracked(make_repo):
    repo = make_repo("a")
    (repo / "-weird.txt").write_text("dashed\n", encoding="utf-8")
    result = await file_diff(repo, await _change(repo, "-weird.txt"))
    assert "+dashed" in result.text


async def test_leading_dash_filename_modified(make_repo, git):
    repo = make_repo("a")
    (repo / "-weird.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "--", "-weird.txt")
    git(repo, "commit", "-m", "dash")
    (repo / "-weird.txt").write_text("two\n", encoding="utf-8")
    result = await file_diff(repo, await _change(repo, "-weird.txt"))
    assert "-one" in result.text and "+two" in result.text


async def test_filename_with_spaces(make_repo, git):
    repo = make_repo("a")
    (repo / "my file.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "my file.txt")
    git(repo, "commit", "-m", "spaces")
    (repo / "my file.txt").write_text("two\n", encoding="utf-8")
    (repo / "other file.txt").write_text("fresh\n", encoding="utf-8")
    modified = await file_diff(repo, await _change(repo, "my file.txt"))
    assert "-one" in modified.text and "+two" in modified.text
    untracked = await file_diff(repo, await _change(repo, "other file.txt"))
    assert "+fresh" in untracked.text


async def test_glob_characters_are_literal(make_repo, git):
    repo = make_repo("a")
    (repo / "[id].tsx").write_text("bracket-1\n", encoding="utf-8")
    (repo / "d.tsx").write_text("sibling-1\n", encoding="utf-8")
    git(repo, "add", "--", "[id].tsx", "d.tsx")
    git(repo, "commit", "-m", "glob")
    (repo / "[id].tsx").write_text("bracket-2\n", encoding="utf-8")
    (repo / "d.tsx").write_text("sibling-2\n", encoding="utf-8")
    result = await file_diff(repo, FileChange(status=" M", path="[id].tsx"))
    assert "+bracket-2" in result.text
    assert "sibling" not in result.text


async def test_non_repo_directory_returns_explanation(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = await file_diff(plain, FileChange(status=" M", path="a.txt"))
    assert isinstance(result.text, str) and result.text
    assert result.path == "a.txt" and result.status == " M"


@pytest.mark.parametrize("exc", [OSError("disk on fire"), RuntimeError("boom"), ValueError()])
async def test_run_git_failures_never_raise(make_repo, monkeypatch, exc):
    async def broken(*args, **kwargs):
        raise exc

    monkeypatch.setattr(diffs, "run_git", broken)
    repo = make_repo("a")
    for status in (" M", "??"):
        result = await file_diff(repo, FileChange(status=status, path="README.md"))
        assert "cannot show README.md" in result.text
        assert (str(exc) or type(exc).__name__) in result.text


async def test_cancellation_still_propagates(make_repo, monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(diffs, "run_git", cancelled)
    repo = make_repo("a")
    with pytest.raises(asyncio.CancelledError):
        await file_diff(repo, FileChange(status=" M", path="README.md"))


async def test_concurrency_is_bounded(make_repo, monkeypatch):
    assert MAX_PARALLEL_DIFFS == 8
    state = {"now": 0, "peak": 0}

    async def fake_run_git(*args, **kwargs):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        try:
            await asyncio.sleep(0.01)
        finally:
            state["now"] -= 1
        return GitResult(code=0, stdout="diff --git a/x b/x\n+x\n", stderr="")

    monkeypatch.setattr(diffs, "run_git", fake_run_git)
    repo = make_repo("a")
    changes = [FileChange(status=" M", path=f"f{i}.txt") for i in range(30)]
    results = await diffs_for(repo, changes)
    assert [r.path for r in results] == [c.path for c in changes]
    assert 1 < state["peak"] <= 8


async def test_git_options_precede_subcommand(make_repo, monkeypatch):
    calls = []

    async def spy(repo, *args, **kwargs):
        calls.append(args)
        return GitResult(code=0, stdout="+x\n", stderr="")

    monkeypatch.setattr(diffs, "run_git", spy)
    repo = make_repo("a")
    await file_diff(repo, FileChange(status=" M", path="README.md"))
    await file_diff(repo, FileChange(status="??", path="new.txt"))
    assert len(calls) == 2
    for args in calls:
        sub = args.index("diff")
        before = args[:sub]
        assert "core.quotePath=false" in before and "-c" in before
        assert "--no-optional-locks" in before and "--literal-pathspecs" in before
        assert "--no-ext-diff" in args[sub:] and "--no-textconv" in args[sub:]


async def test_untracked_directory_does_not_call_git(make_repo, monkeypatch):
    repo = make_repo("a")
    (repo / "newdir").mkdir()
    (repo / "newdir" / "inner.txt").write_text("inner\n", encoding="utf-8")
    change = await _change(repo, "newdir/")
    assert change.status == "??"

    calls = []

    async def spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError("git must not be called")

    monkeypatch.setattr(diffs, "run_git", spy)
    result = await file_diff(repo, change)
    assert result.text == "(untracked directory: newdir/)"
    assert calls == []


@pytest.mark.parametrize(
    "bad",
    [
        "../outside.txt",
        "a/../../b.txt",
        "a/..",
        "..\\x.txt",
        "/etc/passwd",
        "\\\\srv\\x",
        "C:\\x.txt",
        "C:/x.txt",
    ],
)
async def test_unsafe_paths_are_refused_without_git(make_repo, monkeypatch, bad):
    calls = []

    async def spy(*args, **kwargs):
        calls.append(args)
        return GitResult(code=0, stdout="+leak\n", stderr="")

    monkeypatch.setattr(diffs, "run_git", spy)
    repo = make_repo("a")
    for status in ("??", " M"):
        result = await file_diff(repo, FileChange(status=status, path=bad))
        assert "leak" not in result.text
        assert "unsafe path" in result.text
    assert calls == []


async def test_unborn_repo_staged_file(tmp_path, git):
    repo = tmp_path / "unborn"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "first.txt").write_text("first content\n", encoding="utf-8")
    git(repo, "add", "first.txt")
    result = await file_diff(repo, FileChange(status="A ", path="first.txt"))
    assert "+first content" in result.text


async def test_unborn_repo_intent_to_add_uses_worktree_diff(tmp_path, git):
    repo = tmp_path / "unborn2"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "intent.txt").write_text("intent content\n", encoding="utf-8")
    git(repo, "add", "-N", "intent.txt")
    result = await file_diff(repo, FileChange(status=" A", path="intent.txt"))
    assert "+intent content" in result.text


async def test_vanished_untracked_file_reports_error(make_repo):
    repo = make_repo("a")
    victim = repo / "gone.txt"
    victim.write_text("soon gone\n", encoding="utf-8")
    change = await _change(repo, "gone.txt")
    victim.unlink()
    result = await file_diff(repo, change)
    assert "no textual changes" not in result.text
    assert "gone.txt" in result.text
