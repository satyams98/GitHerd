import io
import os
import subprocess

from githerd import interactive
from githerd.interactive import interactive_env, run_git_interactive
from githerd.ui.confirm import confirm_destructive
from githerd.ui.theme import ASCII_GLYPHS, make_console


def ask(answer, **kw):
    console = make_console(io.StringIO(), width=80, force_terminal=False)
    result = confirm_destructive(
        console, command=kw.pop("command", "git reset --hard"),
        repos=kw.pop("repos", ["a", "b"]),
        glyphs=ASCII_GLYPHS, read_line=lambda prompt: answer, **kw,
    )
    return result, console.file.getvalue()


def test_only_explicit_yes_confirms():
    for answer in ("y", "Y", "yes", " YES "):
        assert ask(answer)[0] is True
    for answer in ("", "n", "no", "maybe", "yy"):
        assert ask(answer)[0] is False


def test_eof_and_interrupt_mean_no():
    console = make_console(io.StringIO(), width=80, force_terminal=False)
    for exc in (EOFError, KeyboardInterrupt):
        def reader(prompt, exc=exc):
            raise exc

        assert confirm_destructive(
            console, command="x", repos=["a"], glyphs=ASCII_GLYPHS, read_line=reader
        ) is False


def test_prompt_shows_command_repos_and_detail():
    _, out = ask("n", detail="newer commits will be dropped")
    assert "git reset --hard" in out
    assert "2 repo" in out and "a, b" in out
    assert "newer commits will be dropped" in out


def test_long_repo_lists_are_collapsed():
    _, out = ask("n", repos=[f"r{i}" for i in range(8)])
    assert "r4" in out and "r5" not in out and "+3 more" in out


def test_markup_in_names_commands_and_detail_is_printed_literally():
    _, out = ask(
        "n", command="git clean [bold]-fd[/bold]",
        repos=["[red]x[/red]", "[/oops]"], detail="drops [green]work[/green]",
    )
    assert "[red]x[/red]" in out
    assert "[/oops]" in out
    assert "git clean [bold]-fd[/bold]" in out
    assert "drops [green]work[/green]" in out


def test_ui_text_is_ascii_with_ascii_glyphs():
    _, out = ask("n", repos=[f"r{i}" for i in range(8)], detail="d")
    assert out.isascii()


def test_interactive_env_lifts_the_non_interactive_guards(monkeypatch):
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GCM_INTERACTIVE", "never")
    env = interactive_env()
    assert "GIT_TERMINAL_PROMPT" not in env and "GCM_INTERACTIVE" not in env
    assert "PATH" in env or "Path" in env


def test_interactive_env_does_not_mutate_os_environ(monkeypatch):
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GCM_INTERACTIVE", "never")
    env = interactive_env()
    env["SOMETHING_NEW"] = "1"
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
    assert os.environ["GCM_INTERACTIVE"] == "never"
    assert "SOMETHING_NEW" not in os.environ


def test_run_git_interactive_returns_the_exit_code(make_repo):
    repo = make_repo("a")
    assert run_git_interactive(repo, "rev-parse", "HEAD") == 0
    assert run_git_interactive(repo, "rev-parse", "--verify", "no-such-ref") != 0


def test_run_git_interactive_reports_missing_git(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(interactive.subprocess, "run", boom)
    assert run_git_interactive(tmp_path, "status") == 127


def test_run_git_interactive_attaches_terminal_and_never_echoes(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 3)

    monkeypatch.setattr(interactive.subprocess, "run", fake_run)
    monkeypatch.setenv("SECRET_TOKEN_VALUE", "hunter2-token")
    rc = run_git_interactive(tmp_path, "push", "https://user:pw@example.com/r.git")
    assert rc == 3
    assert seen["cmd"][:3] == ["git", "-C", str(tmp_path)]
    for stream in ("stdin", "stdout", "stderr", "capture_output", "input"):
        assert stream not in seen["kwargs"]
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
