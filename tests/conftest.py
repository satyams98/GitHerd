import subprocess
from pathlib import Path

import pytest


def _run(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture(scope="session", autouse=True)
def isolated_git(tmp_path_factory):
    """Keep tests independent of the developer's global/system git config."""
    cfg = tmp_path_factory.mktemp("gitcfg") / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n",
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GIT_CONFIG_GLOBAL", str(cfg))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


@pytest.fixture
def git():
    return _run


@pytest.fixture
def make_repo(tmp_path):
    def make(name: str, parent: Path | None = None) -> Path:
        remote = tmp_path / "remotes" / f"{name}.git"
        remote.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(remote)],
            check=True, capture_output=True,
        )
        work = (parent or tmp_path / "work") / name
        work.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", str(remote), str(work)], check=True, capture_output=True
        )
        (work / "README.md").write_text("hello\n", encoding="utf-8")
        _run(work, "add", "README.md")
        _run(work, "commit", "-m", "initial")
        _run(work, "push", "-u", "origin", "main")
        return work

    return make


@pytest.fixture
def push_upstream(tmp_path):
    counter = {"n": 0}

    def push(repo: Path, filename: str, content: str = "x\n",
             message: str = "upstream change") -> None:
        remote = _run(repo, "remote", "get-url", "origin")
        counter["n"] += 1
        other = tmp_path / "other" / str(counter["n"])
        other.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", remote, str(other)], check=True, capture_output=True
        )
        (other / filename).write_text(content, encoding="utf-8")
        _run(other, "add", filename)
        _run(other, "commit", "-m", message)
        _run(other, "push", "origin", "main")

    return push


@pytest.fixture
def commit_local():
    def commit(repo: Path, filename: str, content: str = "y\n",
               message: str = "local change") -> None:
        (repo / filename).write_text(content, encoding="utf-8")
        _run(repo, "add", filename)
        _run(repo, "commit", "-m", message)

    return commit
