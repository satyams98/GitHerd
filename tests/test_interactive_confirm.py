import io
import os
import subprocess

import pytest

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
    assert run_git_interactive(repo, "fetch") == 0
    assert run_git_interactive(repo, "fetch", "no-such-remote") != 0


def test_run_git_interactive_reports_missing_git(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(interactive.subprocess, "run", boom)
    assert run_git_interactive(tmp_path, "fetch") == 127


def test_run_git_interactive_attaches_terminal_and_never_echoes(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 3)

    monkeypatch.setattr(interactive.subprocess, "run", fake_run)
    monkeypatch.setenv("SECRET_TOKEN_VALUE", "hunter2-token")
    rc = run_git_interactive(tmp_path, "pull", "https://user:pw@example.com/r.git")
    assert rc == 3
    assert seen["cmd"][:3] == ["git", "-C", str(tmp_path)]
    for stream in ("stdin", "stdout", "stderr", "capture_output", "input"):
        assert stream not in seen["kwargs"]
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_empty_repo_list_is_refused_without_prompting():
    def reader(prompt):
        raise AssertionError("must not prompt when nothing is affected")

    console = make_console(io.StringIO(), width=80, force_terminal=False)
    assert confirm_destructive(
        console, command="git reset --hard", repos=[], glyphs=ASCII_GLYPHS, read_line=reader
    ) is False


def test_eof_and_interrupt_leave_the_cursor_on_a_fresh_line():
    for exc in (EOFError, KeyboardInterrupt):
        buf = io.StringIO()
        console = make_console(buf, width=80, force_terminal=False)

        def reader(prompt, exc=exc):
            buf.write(prompt)  # the real prompt leaves the cursor after its text
            raise exc

        assert confirm_destructive(
            console, command="x", repos=["a"], glyphs=ASCII_GLYPHS, read_line=reader
        ) is False
        console.print("next output")
        assert buf.getvalue().endswith("proceed? [y/N] \nnext output\n")


def test_escape_sequences_in_names_command_and_detail_are_not_printed():
    _, out = ask(
        "n", command="git reset\x1b[2J --hard", repos=["a\x1b]0;x\x07b"], detail="d\x1b[31metail",
    )
    assert "\x1b" not in out and "\x07" not in out
    assert "git reset --hard" in out and "ab" in out and "detail" in out


def test_run_git_interactive_only_allows_fetch_and_pull(monkeypatch, tmp_path):
    def must_not_run(*a, **k):
        raise AssertionError("git must not be started")

    monkeypatch.setattr(interactive.subprocess, "run", must_not_run)
    for args in ((), ("--version",), ("-c", "core.pager=x", "fetch"), ("push",), ("rev-parse", "HEAD"),
                 ("config", "--global", "x", "y"), ("Fetch",), ("fetch-pack",)):
        with pytest.raises(ValueError, match="limited to fetch and pull"):
            run_git_interactive(tmp_path, *args)


def test_run_git_interactive_allows_fetch_and_pull(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        interactive.subprocess, "run",
        lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert run_git_interactive(tmp_path, "fetch") == 0
    assert run_git_interactive(tmp_path, "pull", "--ff-only") == 0
    assert [c[3:] for c in seen] == [["fetch"], ["pull", "--ff-only"]]


def test_run_git_interactive_passes_an_env_without_the_non_interactive_guards(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(interactive.subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GCM_INTERACTIVE", "never")
    run_git_interactive(tmp_path, "fetch")
    assert "GIT_TERMINAL_PROMPT" not in seen["env"]
    assert "GCM_INTERACTIVE" not in seen["env"]
