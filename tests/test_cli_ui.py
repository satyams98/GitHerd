import io
import subprocess

from typer.testing import CliRunner

from githerd import cli
from githerd.cli import app
from githerd.ui.theme import make_console

runner = CliRunner()


def _terminal(monkeypatch):
    """Make the CLI believe it is on an interactive terminal; return the recording console."""
    console = make_console(io.StringIO(), width=100, force_terminal=True, record=True)
    monkeypatch.setattr(cli, "make_console", lambda: console)
    monkeypatch.setattr(cli, "_interactive", lambda c: True)
    return console


def test_status_shows_glyphs_and_sync_counts(make_repo, push_upstream, tmp_path):
    a = make_repo("alpha")
    make_repo("beta")
    push_upstream(a, "x.txt")
    subprocess.run(["git", "-C", str(a), "fetch"], check=True, capture_output=True)
    result = runner.invoke(app, ["status", "--root", str(tmp_path / "work")])
    assert result.exit_code == 0, result.output
    assert "+ alpha" in result.output and "v1" in result.output
    assert "+ beta" in result.output and "clean" in result.output


def test_status_shows_only_the_first_error_line(tmp_path):
    (tmp_path / "broken" / ".git").mkdir(parents=True)  # looks like a repo, is not one
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "x broken" in result.output and "error:" in result.output
    assert "Traceback" not in result.output
    assert len([ln for ln in result.output.splitlines() if "broken" in ln]) >= 1


def test_pull_exit_code_is_2_when_a_repo_needs_attention_non_interactive(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    (a / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(a, "README.md", content="upstream edit\n")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 2
    assert "blocked: 1 local change" in result.output
    assert "1 blocked by local changes" in result.output


def test_pull_exit_code_is_0_when_everything_is_fine(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "n.txt")
    make_repo("b")
    assert runner.invoke(app, ["pull", "--root", str(root)]).exit_code == 0


def test_plain_pull_output_is_ascii_and_one_line_per_repo(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "n.txt")
    make_repo("b")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert result.output.isascii()
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(lines) == 3  # one per repo plus the summary


def test_interactive_pull_draws_the_dashboard(make_repo, push_upstream, monkeypatch, tmp_path):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "n.txt")
    make_repo("b")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 0, result.output
    text = console.export_text()
    assert "Pulling 2 repos" in text and "2/2" in text
    assert "updated: 1 commit, 1 file" in text and "already up to date" in text


def test_interactive_pull_offers_a_card_for_a_blocked_repo(make_repo, push_upstream, monkeypatch, tmp_path):
    console = _terminal(monkeypatch)
    monkeypatch.setattr("githerd.ui.keys.read_key", iter(["k"]).__next__)
    root = tmp_path / "work"
    a = make_repo("a")
    (a / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(a, "README.md", content="upstream edit\n")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 2
    text = console.export_text()
    assert "(blocks pull)" in text and "stash & pull" in text


def test_undo_prints_styled_results(make_repo, push_upstream, git, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "new.txt")
    runner.invoke(app, ["pull", "--root", str(root)])
    result = runner.invoke(app, ["undo", "--root", str(root)])
    assert result.exit_code == 0
    assert "Undoing: pull 1 repos" in result.output
    assert "+ a" in result.output and "restored" in result.output


# ---- controller requirements -------------------------------------------------

def _interrupting(*_args, **_kwargs):
    async def boom(*a, **k):
        raise KeyboardInterrupt

    return boom(*_args, **_kwargs)


def test_ctrl_c_during_plain_pull_exits_130(make_repo, monkeypatch, tmp_path):
    make_repo("a")
    monkeypatch.setattr(cli, "pull_repos", _interrupting)
    result = runner.invoke(app, ["pull", "--root", str(tmp_path / "work")])
    assert result.exit_code == 130
    assert "Interrupted." in result.output
    assert "Traceback" not in result.output


def test_ctrl_c_during_interactive_pull_exits_130(make_repo, monkeypatch, tmp_path):
    console = _terminal(monkeypatch)
    make_repo("a")
    monkeypatch.setattr(cli, "pull_repos", _interrupting)
    result = runner.invoke(app, ["pull", "--root", str(tmp_path / "work")])
    assert result.exit_code == 130
    assert "Interrupted." in console.export_text()


def test_ctrl_c_while_resolving_attention_exits_130(make_repo, monkeypatch, tmp_path):
    console = _terminal(monkeypatch)
    make_repo("a")

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "resolve_attention", boom)
    result = runner.invoke(app, ["pull", "--root", str(tmp_path / "work")])
    assert result.exit_code == 130
    assert "Interrupted." in console.export_text()


def test_ctrl_c_during_undo_exits_130(make_repo, monkeypatch, tmp_path):
    make_repo("a")
    monkeypatch.setattr(cli, "undo_last", _interrupting)
    result = runner.invoke(app, ["undo", "--root", str(tmp_path / "work")])
    assert result.exit_code == 130
    assert "Interrupted." in result.output


def test_ctrl_c_still_journals_what_the_engine_finished(make_repo, push_upstream, monkeypatch, tmp_path):
    """The engine journals in a finally, so an interrupt after real work stays undoable."""
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "n.txt")
    real = cli.pull_repos

    async def finish_then_interrupt(*args, **kwargs):
        await real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "pull_repos", finish_then_interrupt)
    assert runner.invoke(app, ["pull", "--root", str(root)]).exit_code == 130
    monkeypatch.undo()
    undone = runner.invoke(app, ["undo", "--root", str(root)])
    assert undone.exit_code == 0 and "restored" in undone.output


def test_timeout_below_one_is_a_usage_error(tmp_path):
    result = runner.invoke(app, ["pull", "--root", str(tmp_path), "--timeout", "0"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_jobs_below_one_is_a_usage_error(tmp_path):
    assert runner.invoke(app, ["pull", "--root", str(tmp_path), "-j", "0"]).exit_code == 2


def test_pull_help_documents_exit_codes():
    result = runner.invoke(app, ["pull", "--help"])
    assert result.exit_code == 0
    assert "Exit codes" in result.output
    for code in ("0", "2", "130"):
        assert code in result.output
