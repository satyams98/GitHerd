import pytest

from githerd.gitops import classify_failure, fetch, pull
from githerd.outcomes import (
    AuthRequired, BlockedDirty, Diverged, Failed, NetworkError, Ok, UpToDate,
)


async def test_pull_up_to_date(make_repo):
    repo = make_repo("a")
    assert await pull(repo) == UpToDate()


async def test_pull_fast_forwards_new_commits(make_repo, push_upstream, git):
    repo = make_repo("a")
    before = git(repo, "rev-parse", "HEAD")
    push_upstream(repo, "new.txt")
    outcome = await pull(repo)
    assert isinstance(outcome, Ok)
    assert (outcome.commits, outcome.files) == (1, 1)
    assert outcome.before_head == before
    assert outcome.after_head == git(repo, "rev-parse", "HEAD")


async def test_pull_blocked_when_local_changes_would_be_overwritten(make_repo, push_upstream):
    repo = make_repo("a")
    (repo / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(repo, "README.md", content="upstream edit\n")
    outcome = await pull(repo)
    assert isinstance(outcome, BlockedDirty)
    assert [f.path for f in outcome.files] == ["README.md"]


async def test_pull_succeeds_with_unrelated_local_changes(make_repo, push_upstream):
    repo = make_repo("a")
    (repo / "README.md").write_text("local edit\n", encoding="utf-8")
    push_upstream(repo, "other.txt")
    outcome = await pull(repo)
    assert isinstance(outcome, Ok)
    assert (repo / "README.md").read_text(encoding="utf-8") == "local edit\n"


async def test_pull_diverged(make_repo, push_upstream, commit_local):
    repo = make_repo("a")
    commit_local(repo, "local.txt")
    push_upstream(repo, "up.txt")
    assert await pull(repo) == Diverged(ahead=1, behind=1)


async def test_pull_no_upstream(tmp_path, git, commit_local):
    solo = tmp_path / "solo"
    solo.mkdir()
    git(solo, "init", "-b", "main")
    commit_local(solo, "a.txt")
    outcome = await pull(solo)
    assert isinstance(outcome, Failed)
    assert "no upstream" in outcome.message


async def test_pull_detached_head(make_repo, git):
    repo = make_repo("a")
    git(repo, "checkout", "--detach")
    outcome = await pull(repo)
    assert isinstance(outcome, Failed)
    assert "detached" in outcome.message


async def test_fetch_reports_new_upstream_commits(make_repo, push_upstream, git):
    repo = make_repo("a")
    assert await fetch(repo) == UpToDate()
    push_upstream(repo, "new.txt")
    outcome = await fetch(repo)
    head = git(repo, "rev-parse", "HEAD")
    assert outcome == Ok(commits=1, files=0, before_head=head, after_head=head)
    assert await fetch(repo) == UpToDate()


@pytest.mark.parametrize("stderr,expected", [
    ("fatal: Authentication failed for 'https://x/y.git/'", AuthRequired),
    ("fatal: could not read Username for 'https://x': terminal prompts disabled", AuthRequired),
    ("git@x: Permission denied (publickey).\nfatal: Could not read from remote repository.", AuthRequired),
    ("fatal: unable to access 'https://x/': Could not resolve host: x", NetworkError),
    ("ssh: connect to host x port 22: Connection timed out", NetworkError),
    ("fatal: something else entirely", Failed),
])
def test_classify_failure(stderr, expected):
    assert isinstance(classify_failure(stderr, remote="origin"), expected)


async def test_pull_on_non_repo_returns_failed(tmp_path):
    assert isinstance(await pull(tmp_path / "nope"), Failed)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert isinstance(await pull(plain), Failed)


async def test_fetch_on_non_repo_returns_failed(tmp_path):
    assert isinstance(await fetch(tmp_path / "nope"), Failed)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert isinstance(await fetch(plain), Failed)


@pytest.mark.parametrize("url,expected", [
    ("https://user:ghp_SECRET@github.com/o/r.git", "https://github.com/o/r.git"),
    ("https://token@host/x", "https://host/x"),
    ("https://user:pw@host:8443/x.git", "https://host:8443/x.git"),
    ("https://github.com/o/r.git", "https://github.com/o/r.git"),
    ("git@github.com:o/r.git", "git@github.com:o/r.git"),
    ("C:\\repos\\thing", "C:\\repos\\thing"),
    ("/tmp/remote.git", "/tmp/remote.git"),
    ("", ""),
])
def test_redact_url(url, expected):
    from githerd.gitops import redact_url

    assert redact_url(url) == expected


@pytest.mark.parametrize("operation", [pull, fetch])
async def test_outcome_never_contains_url_credentials(operation, make_repo, git):
    repo = make_repo("a")
    git(repo, "config", "remote.origin.url",
        "https://user:ghp_SECRETTOKEN@127.0.0.1:1/o/r.git")
    outcome = await operation(repo)
    assert "ghp_SECRETTOKEN" not in outcome.model_dump_json()
