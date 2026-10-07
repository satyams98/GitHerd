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
