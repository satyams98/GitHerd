import githerd


def test_version():
    assert githerd.__version__ == "0.1.0"


def test_make_repo_has_upstream(make_repo, git):
    repo = make_repo("alpha")
    assert git(repo, "rev-parse", "--abbrev-ref", "@{u}") == "origin/main"
