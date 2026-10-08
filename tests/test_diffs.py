from githerd import diffs
from githerd.diffs import diffs_for, file_diff
from githerd.outcomes import FileChange
from githerd.repos import snapshot


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
    assert isinstance(results["naïve-日本.txt"].text, str) and results["naïve-日本.txt"].text
    assert isinstance(results["blob.bin"].text, str) and results["blob.bin"].text
