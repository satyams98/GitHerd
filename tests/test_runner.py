import pytest

from githerd.runner import parse_progress, run_git


@pytest.mark.parametrize("line,expected", [
    ("Receiving objects:  78% (78/100)", ("Receiving objects", 78)),
    ("remote: Counting objects: 100% (5/5), done.", ("Counting objects", 100)),
    ("Resolving deltas:   5% (1/20)", ("Resolving deltas", 5)),
    ("Already up to date.", None),
    ("From /tmp/remote", None),
])
def test_parse_progress(line, expected):
    assert parse_progress(line) == expected


async def test_run_git_success(make_repo):
    repo = make_repo("a")
    result = await run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert result.ok
    assert result.stdout.strip() == "main"


async def test_run_git_failure_returns_result_not_exception(tmp_path):
    result = await run_git(tmp_path, "rev-parse", "HEAD")
    assert not result.ok
    assert result.code != 0
    assert result.stderr


async def test_run_git_streams_progress_lines(make_repo, tmp_path):
    repo = make_repo("a")
    remote = (await run_git(repo, "remote", "get-url", "origin")).stdout.strip()
    lines: list[str] = []
    result = await run_git(
        tmp_path, "clone", "--progress", remote, str(tmp_path / "dest"),
        on_progress=lines.append,
    )
    assert result.ok
    assert any("Cloning into" in line for line in lines)
