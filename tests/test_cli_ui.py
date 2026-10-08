import asyncio
import io
import signal
import subprocess

import pytest
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
    assert len([ln for ln in result.output.splitlines() if "broken" in ln]) == 1


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
    assert len(lines) == 4  # one per repo, the summary and the undo hint
    assert lines[-1] == "undo available: githerd undo"


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


# ---- review fixes -------------------------------------------------------------

def _fake_snapshots(monkeypatch, snaps):
    async def fake(repos):
        return snaps

    monkeypatch.setattr(cli, "snapshot_all", fake)


def test_status_separates_sync_and_changes_columns(make_repo, push_upstream, tmp_path):
    a = make_repo("alpha")
    make_repo("beta")
    push_upstream(a, "x.txt")
    subprocess.run(["git", "-C", str(a), "fetch"], check=True, capture_output=True)
    (a / "scratch.txt").write_text("s\n", encoding="utf-8")
    result = runner.invoke(app, ["status", "--root", str(tmp_path / "work")])
    assert result.exit_code == 0, result.output
    assert "v1  1 changed" in result.output
    assert "upstreamclean" not in result.output and "changedclean" not in result.output


def test_status_without_upstream_is_separated_from_changes(tmp_path, git):
    repo = tmp_path / "solo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "no upstream  clean" in result.output


def test_plain_status_is_one_unwrapped_line_per_repo_with_full_names(make_repo, monkeypatch, tmp_path):
    long_name = "r" * 40
    make_repo(long_name)
    make_repo("short")
    (tmp_path / "work" / "broken" / ".git").mkdir(parents=True)
    from githerd.repos import RepoSnapshot

    real = cli.snapshot_all

    async def with_long_error(repos):
        snaps = await real(repos)
        return [
            RepoSnapshot(path=s.path, name=s.name, error="fatal: " + "boom " * 60) if s.name == "broken" else s
            for s in snaps
        ]

    monkeypatch.setattr(cli, "snapshot_all", with_long_error)
    result = runner.invoke(app, ["status", "--root", str(tmp_path / "work")])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(lines) == 3, result.output
    assert any(long_name in ln and "..." not in ln for ln in lines)
    broken = [ln for ln in lines if "broken" in ln]
    assert len(broken) == 1 and broken[0].rstrip().endswith("boom")  # not wrapped at 80 columns
    assert len(broken[0]) > 80


def test_terminal_status_still_caps_long_names(make_repo, monkeypatch, tmp_path):
    console = _terminal(monkeypatch)
    long_name = "r" * 40
    make_repo(long_name)
    result = runner.invoke(app, ["status", "--root", str(tmp_path / "work")])
    assert result.exit_code == 0, result.output
    text = console.export_text()
    assert long_name not in text and "r" * 20 in text


def test_ctrl_c_during_status_exits_130(make_repo, monkeypatch, tmp_path):
    make_repo("a")
    monkeypatch.setattr(cli, "snapshot_all", _interrupting)
    result = runner.invoke(app, ["status", "--root", str(tmp_path / "work")])
    assert result.exit_code == 130
    assert "Interrupted." in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("value", ["inf", "nan"])
def test_non_finite_timeout_is_a_usage_error(tmp_path, value):
    result = runner.invoke(app, ["pull", "--root", str(tmp_path), "--timeout", value])
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    unboxed = result.output.replace("│", " ")  # typer draws errors in a box that wraps the text
    assert "timeout must be a finite number of seconds >= 1" in " ".join(unboxed.split())


def test_negative_infinite_timeout_is_a_usage_error(tmp_path):
    result = runner.invoke(app, ["pull", "--root", str(tmp_path), "--timeout", "-inf"])
    assert result.exit_code == 2 and "Traceback" not in result.output


def test_undo_exit_code_is_0_when_nothing_to_undo(make_repo, tmp_path):
    make_repo("a")
    result = runner.invoke(app, ["undo", "--root", str(tmp_path / "work")])
    assert result.exit_code == 0
    assert "Nothing to undo." in result.output


def test_undo_exit_code_is_2_when_a_moved_repo_is_skipped(make_repo, push_upstream, commit_local, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "new.txt")
    assert runner.invoke(app, ["pull", "--root", str(root)]).exit_code == 0
    commit_local(a, "mine.txt")
    result = runner.invoke(app, ["undo", "--root", str(root)])
    assert result.exit_code == 2, result.output
    assert "skipped" in result.output


def test_undo_exit_code_is_2_when_a_repo_fails(make_repo, monkeypatch, tmp_path):
    from githerd.journal import OpSet
    from githerd.undo import UndoItem

    async def fake_undo(root, journal, **kwargs):
        op = OpSet(id="x", description="pull 1 repos", timestamp="t", entries=[])
        return op, [UndoItem(repo=str(tmp_path / "a"), status="failed", detail="boom")]

    make_repo("a")
    monkeypatch.setattr(cli, "undo_last", fake_undo)
    result = runner.invoke(app, ["undo", "--root", str(tmp_path / "work")])
    assert result.exit_code == 2, result.output
    assert "failed" in result.output


def test_undo_help_documents_exit_codes():
    result = runner.invoke(app, ["undo", "--help"])
    assert result.exit_code == 0
    assert "Exit codes" in result.output
    assert "0" in result.output and "2" in result.output


def _pulled_and_moved(make_repo, push_upstream, commit_local, git, runner_, root):
    a = make_repo("a")
    before = git(a, "rev-parse", "HEAD")
    push_upstream(a, "new.txt")
    assert runner_.invoke(app, ["pull", "--root", str(root)]).exit_code == 0
    commit_local(a, "mine.txt")
    return a, before


def test_interactive_undo_confirmed_resets_a_moved_repo(
    make_repo, push_upstream, commit_local, git, monkeypatch, tmp_path
):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    a, before = _pulled_and_moved(make_repo, push_upstream, commit_local, git, runner, root)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    result = runner.invoke(app, ["undo", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert git(a, "rev-parse", "HEAD") == before
    text = console.export_text()
    assert f"git reset --keep {before[:7]}" in text
    assert "(a)" in text and "restored" in text


def test_interactive_undo_declined_leaves_the_repo_and_exits_2(
    make_repo, push_upstream, commit_local, git, monkeypatch, tmp_path
):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    a, before = _pulled_and_moved(make_repo, push_upstream, commit_local, git, runner, root)
    moved_head = git(a, "rev-parse", "HEAD")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    result = runner.invoke(app, ["undo", "--root", str(root)])
    assert result.exit_code == 2, result.output
    assert git(a, "rev-parse", "HEAD") == moved_head != before
    text = console.export_text()
    assert "git reset --keep" in text and "skipped" in text


def test_real_sigint_during_plain_pull_exits_130(make_repo, monkeypatch, tmp_path):
    make_repo("a")

    async def sigint_then_wait(*args, **kwargs):
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(5)

    monkeypatch.setattr(cli, "pull_repos", sigint_then_wait)
    result = runner.invoke(app, ["pull", "--root", str(tmp_path / "work")])
    assert result.exit_code == 130, result.output
    assert "Interrupted." in result.output
    assert "Traceback" not in result.output


# ---- undo hint after a pull that moved something -----------------------------------

UNDO_HINT = "undo available: githerd undo"


def test_plain_pull_prints_the_undo_hint_when_a_repo_was_updated(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "n.txt")
    make_repo("b")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert UNDO_HINT in result.output
    assert result.output.isascii()
    assert result.output.strip().splitlines()[-1] == UNDO_HINT


def test_plain_pull_prints_no_undo_hint_when_nothing_moved(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    make_repo("a")
    b = make_repo("b")
    (b / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(b, "README.md", content="upstream edit\n")  # blocked, not updated
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 2
    assert UNDO_HINT not in result.output


def test_interactive_pull_prints_the_undo_hint_when_a_repo_was_updated(make_repo, push_upstream, monkeypatch, tmp_path):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "n.txt")
    make_repo("b")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert UNDO_HINT in console.export_text()


def test_interactive_pull_prints_no_undo_hint_when_nothing_moved(make_repo, monkeypatch, tmp_path):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    make_repo("a")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert UNDO_HINT not in console.export_text()


def test_plain_pull_line_for_a_hostile_repo_name_is_clean_and_aligned(monkeypatch, tmp_path):
    from pathlib import Path

    from githerd.bulk import RepoEvent
    from githerd.outcomes import UpToDate, describe

    evil = tmp_path / "evil\u202e\x1b[2Jname"  # never touches the file system
    other = tmp_path / "b"
    monkeypatch.setattr(cli, "_repos", lambda root: (tmp_path, [evil, other]))

    async def fake_pull_repos(base, repos, *, concurrency=5, on_event=None, timeout=None):
        for repo in repos:
            on_event(RepoEvent(repo=str(repo), kind="done", outcome=UpToDate()))
        return {repo: UpToDate() for repo in repos}

    monkeypatch.setattr(cli, "pull_repos", fake_pull_repos)
    result = runner.invoke(app, ["pull", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "\u202e" not in result.output
    done = describe(UpToDate())
    lines = [ln for ln in result.output.splitlines() if ln.endswith(done)]
    assert lines == [f"evil?name  {done}", f"{'b':<9}  {done}"]


# ---- H2 item 5: the undo confirmation names the branch and the dropped commits -------

def test_interactive_undo_confirmation_names_the_branch_and_one_dropped_commit(
    make_repo, push_upstream, commit_local, git, monkeypatch, tmp_path
):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    _pulled_and_moved(make_repo, push_upstream, commit_local, git, runner, root)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    result = runner.invoke(app, ["undo", "--root", str(root)])
    assert result.exit_code == 2, result.output
    assert "a is on 'main'; 1 newer commit will be dropped from it (kept in the reflog)" in console.export_text()


def test_interactive_undo_confirmation_counts_several_dropped_commits(
    make_repo, push_upstream, commit_local, git, monkeypatch, tmp_path
):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    a, _ = _pulled_and_moved(make_repo, push_upstream, commit_local, git, runner, root)
    commit_local(a, "mine2.txt")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    runner.invoke(app, ["undo", "--root", str(root)])
    assert "a is on 'main'; 2 newer commits will be dropped from it (kept in the reflog)" in console.export_text()


def test_interactive_undo_confirmation_says_some_when_the_count_is_unavailable(
    make_repo, push_upstream, commit_local, git, monkeypatch, tmp_path
):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    _pulled_and_moved(make_repo, push_upstream, commit_local, git, runner, root)
    monkeypatch.setattr(cli, "git_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    runner.invoke(app, ["undo", "--root", str(root)])
    assert "a is on 'main'; some newer commits will be dropped from it (kept in the reflog)" in console.export_text()


# ---- H4 item 2: the interactive actions get the --timeout value -----------------------------

def _capture_resolve(monkeypatch):
    seen = {}

    def fake(console, root, results, glyphs, **kwargs):
        seen.update(kwargs)
        return results

    monkeypatch.setattr(cli, "resolve_attention", fake)
    return seen


def test_pull_passes_its_timeout_to_the_attention_actions(make_repo, monkeypatch, tmp_path):
    _terminal(monkeypatch)
    seen = _capture_resolve(monkeypatch)
    make_repo("a")
    result = runner.invoke(app, ["pull", "--root", str(tmp_path / "work"), "--timeout", "45"])
    assert result.exit_code == 0, result.output
    assert seen["timeout"] == 45.0


def test_pull_passes_the_default_timeout_when_none_is_given(make_repo, monkeypatch, tmp_path):
    _terminal(monkeypatch)
    seen = _capture_resolve(monkeypatch)
    make_repo("a")
    assert runner.invoke(app, ["pull", "--root", str(tmp_path / "work")]).exit_code == 0
    assert seen["timeout"] == 300.0


# ---- H5: the real git command is shown quietly, on an interactive terminal only ------------------

PULL_COMMAND = "git pull --ff-only --progress"


def _styled_terminal(monkeypatch):
    """Like ``_terminal`` but with colour support, so ``export_text(styles=True)`` shows styles."""
    from rich.console import Console

    from githerd.ui.theme import THEME

    console = Console(
        file=io.StringIO(), width=100, force_terminal=True, color_system="standard", record=True,
        theme=THEME, highlight=False, emoji=False,
    )
    monkeypatch.setattr(cli, "make_console", lambda: console)
    monkeypatch.setattr(cli, "_interactive", lambda c: True)
    return console


def test_interactive_pull_shows_the_pull_command_under_the_dashboard(make_repo, push_upstream, monkeypatch, tmp_path):
    console = _styled_terminal(monkeypatch)
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "n.txt")
    make_repo("b")
    assert runner.invoke(app, ["pull", "--root", str(root)]).exit_code == 0
    plain = console.export_text(clear=False)
    assert "$ " + PULL_COMMAND in plain
    assert "\x1b[2m" in console.export_text(styles=True)  # drawn dim


def test_plain_pull_output_has_no_command_lines(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "n.txt")
    make_repo("b")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert "$" not in result.output and PULL_COMMAND not in result.output
    assert len([ln for ln in result.output.splitlines() if ln.strip()]) == 4


def test_plain_pull_with_a_blocked_repo_still_prints_no_command_lines(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    (a / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(a, "README.md", content="upstream edit\n")
    result = runner.invoke(app, ["pull", "--root", str(root)])
    assert result.exit_code == 2
    assert "$ git" not in result.output


def test_interactive_undo_shows_the_reset_command_under_each_restored_item(
    make_repo, push_upstream, git, monkeypatch, tmp_path
):
    root = tmp_path / "work"
    a = make_repo("a")
    before = git(a, "rev-parse", "HEAD")
    push_upstream(a, "new.txt")
    assert runner.invoke(app, ["pull", "--root", str(root)]).exit_code == 0
    console = _styled_terminal(monkeypatch)
    result = runner.invoke(app, ["undo", "--root", str(root)])
    assert result.exit_code == 0, result.output
    command = f"    $ git reset --keep {before[:7]}"
    lines = console.export_text(clear=False).splitlines()
    (index,) = [i for i, ln in enumerate(lines) if "restored" in ln]
    assert lines[index + 1] == command
    assert "\x1b[2m" + command in console.export_text(styles=True)


def test_interactive_undo_shows_no_command_for_a_skipped_item(
    make_repo, push_upstream, commit_local, git, monkeypatch, tmp_path
):
    console = _terminal(monkeypatch)
    root = tmp_path / "work"
    _pulled_and_moved(make_repo, push_upstream, commit_local, git, runner, root)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert runner.invoke(app, ["undo", "--root", str(root)]).exit_code == 2
    assert "    $ git reset --keep" not in console.export_text()  # only the confirmation names it


def test_interactive_undo_skips_the_command_line_when_there_is_no_sha(monkeypatch, tmp_path):
    from githerd.journal import OpSet
    from githerd.undo import UndoItem

    async def fake_undo(root, journal, **kwargs):
        op = OpSet(id="x", description="pull 1 repos", timestamp="t", entries=[])
        return op, [UndoItem(repo=str(tmp_path / "a"), status="restored", detail="")]

    console = _terminal(monkeypatch)
    monkeypatch.setattr(cli, "undo_last", fake_undo)
    assert runner.invoke(app, ["undo", "--root", str(tmp_path)]).exit_code == 0
    text = console.export_text()
    assert "restored" in text and "$ git" not in text


def test_plain_undo_output_has_no_command_lines(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    push_upstream(make_repo("a"), "new.txt")
    runner.invoke(app, ["pull", "--root", str(root)])
    result = runner.invoke(app, ["undo", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert "restored" in result.output and "$" not in result.output


# ---- H6 item 4: terminal status error lines never wrap ------------------------------------------

def test_terminal_status_error_lines_are_fitted_to_the_width(monkeypatch, tmp_path):
    from rich.cells import cell_len

    from githerd.repos import RepoSnapshot

    console = _terminal(monkeypatch)
    (tmp_path / "broken" / ".git").mkdir(parents=True)

    async def long_error(repos):
        return [RepoSnapshot(path=repos[0], name="broken", error="fatal: " + "boom " * 60)]

    monkeypatch.setattr(cli, "snapshot_all", long_error)
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in console.export_text().splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert cell_len(lines[0]) <= 100 and "broken" in lines[0]
    assert lines[0].rstrip().endswith(("…", "..."))


# ---- H6 item 5: a snapshot that errored shows '?' as its branch --------------------------------------

def test_dashboard_branch_is_a_question_mark_for_a_repo_whose_snapshot_errored(monkeypatch, tmp_path):
    console = _terminal(monkeypatch)
    monkeypatch.setattr("githerd.ui.keys.read_key", iter(["k", "k", "k"]).__next__)
    (tmp_path / "broken" / ".git").mkdir(parents=True)  # looks like a repo, is not one
    result = runner.invoke(app, ["pull", "--root", str(tmp_path)])
    assert result.exit_code == 2, result.output
    text = console.export_text()
    assert "(detached)" not in text
    assert any("broken  ?  " in ln for ln in text.splitlines()), text  # the dashboard row's branch column


# ---- H6 item 6: duplicate repo names are told apart ------------------------------------------------------

def _duplicate_repos(tmp_path):
    repos = [tmp_path / "team-a" / "api", tmp_path / "b" / "api", tmp_path / "docs"]
    for repo in repos:
        (repo / ".git").mkdir(parents=True)
    return repos


def test_plain_pull_lines_use_relative_paths_for_duplicate_names(monkeypatch, tmp_path):
    from githerd.bulk import RepoEvent
    from githerd.outcomes import UpToDate, describe

    repos = _duplicate_repos(tmp_path)
    monkeypatch.setattr(cli, "_repos", lambda root: (tmp_path, repos))

    async def fake_pull_repos(base, repos, *, concurrency=5, on_event=None, timeout=None):
        for repo in repos:
            on_event(RepoEvent(repo=str(repo), kind="done", outcome=UpToDate()))
        return {repo: UpToDate() for repo in repos}

    monkeypatch.setattr(cli, "pull_repos", fake_pull_repos)
    result = runner.invoke(app, ["pull", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    done = describe(UpToDate())
    lines = [ln for ln in result.output.splitlines() if ln.endswith(done)]
    assert lines == [f"team-a/api  {done}", f"b/api       {done}", f"docs        {done}"]


def test_plain_pull_lines_for_unique_names_are_unchanged(monkeypatch, tmp_path):
    from githerd.bulk import RepoEvent
    from githerd.outcomes import UpToDate, describe

    repos = [tmp_path / "team-a" / "api", tmp_path / "docs"]
    monkeypatch.setattr(cli, "_repos", lambda root: (tmp_path, repos))

    async def fake_pull_repos(base, repos, *, concurrency=5, on_event=None, timeout=None):
        for repo in repos:
            on_event(RepoEvent(repo=str(repo), kind="done", outcome=UpToDate()))
        return {repo: UpToDate() for repo in repos}

    monkeypatch.setattr(cli, "pull_repos", fake_pull_repos)
    result = runner.invoke(app, ["pull", "--root", str(tmp_path)])
    done = describe(UpToDate())
    assert [ln for ln in result.output.splitlines() if ln.endswith(done)] == [
        f"api   {done}", f"docs  {done}",
    ]


def test_status_uses_relative_paths_for_duplicate_names(tmp_path):
    _duplicate_repos(tmp_path)
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(ln.startswith("x team-a/api") for ln in lines)
    assert any(ln.startswith("x b/api") for ln in lines)
    assert any(ln.startswith("x docs") for ln in lines)


def test_interactive_dashboard_shows_relative_paths_for_duplicate_names(monkeypatch, tmp_path):
    from githerd.bulk import RepoEvent
    from githerd.outcomes import UpToDate

    console = _terminal(monkeypatch)
    repos = _duplicate_repos(tmp_path)
    monkeypatch.setattr(cli, "_repos", lambda root: (tmp_path, repos))

    async def fake_pull_repos(base, repos, *, concurrency=5, on_event=None, timeout=None):
        for repo in repos:
            on_event(RepoEvent(repo=str(repo), kind="start"))
            on_event(RepoEvent(repo=str(repo), kind="done", outcome=UpToDate()))
        return {repo: UpToDate() for repo in repos}

    monkeypatch.setattr(cli, "pull_repos", fake_pull_repos)
    assert runner.invoke(app, ["pull", "--root", str(tmp_path)]).exit_code == 0
    text = console.export_text()
    assert "team-a/api" in text and "b/api" in text and "docs" in text


def test_pull_hands_the_labels_to_the_attention_loop(monkeypatch, tmp_path):
    _terminal(monkeypatch)
    seen = _capture_resolve(monkeypatch)
    repos = _duplicate_repos(tmp_path)
    monkeypatch.setattr(cli, "_repos", lambda root: (tmp_path, repos))
    assert runner.invoke(app, ["pull", "--root", str(tmp_path)]).exit_code in (0, 2)
    assert seen["labels"] == {repos[0]: "team-a/api", repos[1]: "b/api", repos[2]: "docs"}


def test_undo_output_uses_relative_paths_for_duplicate_names(monkeypatch, tmp_path):
    from githerd.journal import JournalEntry, OpSet
    from githerd.undo import UndoItem

    repos = _duplicate_repos(tmp_path)

    async def fake_undo(root, journal, **kwargs):
        op = OpSet(id="x", description="pull 3 repos", timestamp="t", entries=[
            JournalEntry(repo=str(r), op="pull", before_head="a" * 40, after_head="b" * 40) for r in repos
        ])
        return op, [UndoItem(repo=str(r), status="restored", detail=f"back to {'a' * 7}") for r in repos]

    monkeypatch.setattr(cli, "undo_last", fake_undo)
    result = runner.invoke(app, ["undo", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(ln.startswith("+ team-a/api  restored") for ln in lines)
    assert any(ln.startswith("+ b/api  restored") for ln in lines)
    assert any(ln.startswith("+ docs  restored") for ln in lines)


# ---- H6 item 7: plain undo lines never wrap at 80 columns ---------------------------------------------------

def test_plain_undo_lines_do_not_wrap_at_80_columns(monkeypatch, tmp_path):
    from githerd.journal import OpSet
    from githerd.undo import UndoItem

    long_dir = "r" * 70
    detail = "repo is on branch 'feature' but the operation was on 'main'; not undone"

    async def fake_undo(root, journal, **kwargs):
        op = OpSet(id="x", description="pull 2 repos", timestamp="t", entries=[])
        return op, [
            UndoItem(repo=str(tmp_path / long_dir), status="skipped", detail=detail),
            UndoItem(repo=str(tmp_path / "b"), status="restored", detail="back to 1234567"),
        ]

    monkeypatch.setattr(cli, "undo_last", fake_undo)
    result = runner.invoke(app, ["undo", "--root", str(tmp_path)])
    assert result.exit_code == 2, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(lines) == 3, result.output  # the heading plus one line per item
    skipped = [ln for ln in lines if long_dir in ln]
    assert len(skipped) == 1 and skipped[0].endswith("not undone") and len(skipped[0]) > 80
