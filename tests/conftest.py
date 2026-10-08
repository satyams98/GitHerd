import re
import shutil
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


@pytest.fixture(autouse=True)
def _clean_ui_env(monkeypatch):
    """A developer's shell must not change rendering results."""
    for var in ("GITHERD_ASCII", "NO_COLOR", "FORCE_COLOR", "COLUMNS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory, isolated_git):
    """One bare remote + clone (one commit pushed, upstream set), built once.

    Every make_repo() call copies these two trees, which is ~20x cheaper than
    spawning the ~7 git processes needed to build a repo from scratch.
    """
    root = tmp_path_factory.mktemp("repo_template")
    remote = root / "remotes" / "template.git"
    remote.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    work = root / "work" / "template"
    work.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", str(remote), str(work)], check=True, capture_output=True
    )
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _run(work, "add", "README.md")
    _run(work, "commit", "-m", "initial")
    _run(work, "push", "-u", "origin", "main")
    _run(work, "update-index", "--refresh")  # index stat info is up to date
    return remote, work


_URL_LINE = re.compile(r"^([ \t]*url[ \t]*=[ \t]*).*$", re.MULTILINE)


def _point_origin_at(work: Path, remote: Path) -> None:
    """Rewrite the clone's origin URL in .git/config without spawning git.

    The path is written as git itself writes it (str(remote)), with backslashes
    escaped the way git config values require.
    """
    cfg = work / ".git" / "config"
    text = cfg.read_text(encoding="utf-8")
    url = str(remote).replace("\\", "\\\\").replace('"', '\\"')
    new, count = _URL_LINE.subn(lambda m: m.group(1) + url, text)
    assert count == 1, f"expected exactly one remote url in {cfg}"
    cfg.write_bytes(new.encode("utf-8"))


@pytest.fixture
def make_repo(tmp_path, _repo_template):
    template_remote, template_work = _repo_template

    def make(name: str, parent: Path | None = None) -> Path:
        remote = tmp_path / "remotes" / f"{name}.git"
        remote.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template_remote, remote)
        work = (parent or tmp_path / "work") / name
        work.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template_work, work)
        _point_origin_at(work, remote)
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
