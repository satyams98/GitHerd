from typer.testing import CliRunner

from githerd.cli import app

runner = CliRunner()


def test_status_lists_repos(make_repo, tmp_path):
    make_repo("alpha")
    beta = make_repo("beta")
    (beta / "README.md").write_text("dirty\n", encoding="utf-8")
    result = runner.invoke(app, ["status", "--root", str(tmp_path / "work")])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output and "clean" in result.output
    assert "beta" in result.output and "1 changed" in result.output


def test_status_with_no_repos_exits_1(tmp_path):
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "No git repositories found" in result.output


def test_pull_then_undo_end_to_end(make_repo, push_upstream, git, tmp_path):
    root = tmp_path / "work"
    a, b = make_repo("a"), make_repo("b")
    before = git(a, "rev-parse", "HEAD")
    push_upstream(a, "new.txt")

    pulled = runner.invoke(app, ["pull", "--root", str(root)])
    assert pulled.exit_code == 0, pulled.output
    assert "updated: 1 commit, 1 file" in pulled.output
    assert "already up to date" in pulled.output
    assert "1 updated, 1 up to date" in pulled.output
    assert git(a, "rev-parse", "HEAD") != before

    undone = runner.invoke(app, ["undo", "--root", str(root)])
    assert undone.exit_code == 0, undone.output
    assert "restored" in undone.output
    assert git(a, "rev-parse", "HEAD") == before

    again = runner.invoke(app, ["undo", "--root", str(root)])
    assert "restored" in again.output  # undo of the undo re-applies the pull
    nothing = runner.invoke(app, ["undo", "--root", str(root)])
    assert "Nothing to undo" in nothing.output


import os
import subprocess
import sys

import pytest


def _no_git(monkeypatch, tmp_path):
    monkeypatch.setattr("githerd.cli.shutil.which", lambda *a, **k: None)
    empty = tmp_path / "empty-path"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


@pytest.mark.parametrize("command", ["status", "pull", "undo"])
def test_missing_git_exits_1_without_traceback(command, monkeypatch, tmp_path):
    _no_git(monkeypatch, tmp_path)
    result = runner.invoke(app, [command, "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "git executable not found; install Git for Windows" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_help_works_without_git(monkeypatch, tmp_path):
    _no_git(monkeypatch, tmp_path)
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.output


def test_status_with_non_ascii_repo_name(make_repo, tmp_path):
    make_repo("日本語-repo")
    result = runner.invoke(app, ["status", "--root", str(tmp_path / "work")])
    assert result.exit_code == 0, result.output
    assert "日本語-repo" in result.output


def test_status_non_ascii_when_piped_in_subprocess(make_repo, tmp_path):
    make_repo("日本語-repo")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    proc = subprocess.run(
        [sys.executable, "-m", "githerd.cli", "status", "--root", str(tmp_path / "work")],
        capture_output=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert "日本語-repo" in proc.stdout.decode("utf-8", errors="replace")


def test_status_shows_only_first_line_of_error(monkeypatch, tmp_path):
    from githerd.repos import RepoSnapshot

    (tmp_path / "x" / ".git").mkdir(parents=True)

    async def fake_all(repos):
        return [RepoSnapshot(path=repos[0], name="x", error="first line\nsecond line")]

    monkeypatch.setattr("githerd.cli.snapshot_all", fake_all)
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert "first line" in result.output
    assert "second line" not in result.output


def test_undo_output_has_no_dangling_colon(monkeypatch, tmp_path):
    from githerd.journal import OpSet
    from githerd.undo import UndoItem

    async def fake_undo(root, journal):
        op = OpSet(id="1", timestamp="t", description="d", entries=[])
        return op, [UndoItem(repo=str(tmp_path / "r"), status="restored", detail="")]

    monkeypatch.setattr("githerd.cli.undo_last", fake_undo)
    result = runner.invoke(app, ["undo", "--root", str(tmp_path)])
    assert "r  restored" in result.output
    assert "restored:" not in result.output
