from pathlib import Path

from githerd.outcomes import FileChange
from githerd.repos import discover_repos, parse_status_v2, snapshot, snapshot_all


def _fake_repo(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def test_discover_finds_nested_repos_sorted(tmp_path):
    _fake_repo(tmp_path / "b")
    _fake_repo(tmp_path / "group" / "a")
    assert discover_repos(tmp_path) == [tmp_path / "b", tmp_path / "group" / "a"]


def test_discover_does_not_descend_into_repos(tmp_path):
    _fake_repo(tmp_path / "outer")
    _fake_repo(tmp_path / "outer" / "vendor" / "inner")
    assert discover_repos(tmp_path) == [tmp_path / "outer"]


def test_discover_skips_ignored_and_hidden_dirs(tmp_path):
    _fake_repo(tmp_path / "node_modules" / "pkg")
    _fake_repo(tmp_path / ".hidden" / "x")
    _fake_repo(tmp_path / "real")
    assert discover_repos(tmp_path) == [tmp_path / "real"]


def test_discover_respects_max_depth(tmp_path):
    _fake_repo(tmp_path / "a" / "b" / "c" / "d" / "e")  # depth 5
    assert discover_repos(tmp_path, max_depth=4) == []
    assert discover_repos(tmp_path, max_depth=5) == [tmp_path / "a" / "b" / "c" / "d" / "e"]


def test_discover_root_itself_a_repo(tmp_path):
    _fake_repo(tmp_path)
    assert discover_repos(tmp_path) == [tmp_path]


def test_parse_status_v2_handles_all_entry_types():
    raw = (
        "# branch.oid abc123\0# branch.head main\0# branch.upstream origin/main\0"
        "# branch.ab +1 -2\0"
        "1 .M N... 100644 100644 100644 h1 h2 file with space.txt\0"
        "2 R. N... 100644 100644 100644 h1 h2 R100 new.txt\0old.txt\0"
        "u UU N... 100644 100644 100644 100644 h1 h2 h3 conflict.txt\0"
        "? untracked.txt\0"
    )
    info = parse_status_v2(raw)
    assert info["branch"] == "main"
    assert info["head"] == "abc123"
    assert info["upstream"] == "origin/main"
    assert (info["ahead"], info["behind"]) == (1, 2)
    assert info["dirty"] == [
        FileChange(status=" M", path="file with space.txt"),
        FileChange(status="R ", path="new.txt"),
        FileChange(status="UU", path="conflict.txt"),
        FileChange(status="??", path="untracked.txt"),
    ]


def test_parse_status_v2_detached_and_initial():
    info = parse_status_v2("# branch.oid (initial)\0# branch.head (detached)\0")
    assert info["branch"] is None
    assert info["head"] == ""
    assert info["upstream"] is None


async def test_snapshot_clean_repo(make_repo):
    repo = make_repo("alpha")
    snap = await snapshot(repo)
    assert snap.name == "alpha"
    assert snap.branch == "main"
    assert snap.upstream == "origin/main"
    assert (snap.ahead, snap.behind) == (0, 0)
    assert snap.dirty == []
    assert snap.last_commit == "initial"
    assert not snap.is_dirty
    assert len(snap.head) == 40


async def test_snapshot_dirty_repo(make_repo, git):
    repo = make_repo("alpha")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    (repo / "staged.txt").write_text("s\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    snap = await snapshot(repo)
    assert sorted((c.status, c.path) for c in snap.dirty) == [
        (" M", "README.md"), ("??", "new.txt"), ("A ", "staged.txt"),
    ]
    assert snap.is_dirty


async def test_snapshot_ahead_and_behind(make_repo, git, commit_local, push_upstream):
    repo = make_repo("alpha")
    commit_local(repo, "local.txt")
    push_upstream(repo, "up.txt")
    git(repo, "fetch")
    snap = await snapshot(repo)
    assert (snap.ahead, snap.behind) == (1, 1)


async def test_snapshot_all_isolates_failures(make_repo, tmp_path):
    good = make_repo("good")
    bad = tmp_path / "not-a-repo"
    bad.mkdir()
    snaps = await snapshot_all([good, bad])
    assert snaps[0].error == ""
    assert snaps[1].error != ""
    assert snaps[1].name == "not-a-repo"
