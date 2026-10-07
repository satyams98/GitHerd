import pytest

from githerd.safety import Tier, classify

R, M, D = Tier.READ, Tier.MUTATE, Tier.DESTRUCTIVE


@pytest.mark.parametrize("args,tier", [
    # read
    (["status"], R), (["log", "--oneline"], R), (["-C", "x", "diff"], R),
    (["fetch", "--all"], R), (["blame", "a.py"], R), (["show", "HEAD"], R),
    (["branch"], R), (["branch", "-a"], R), (["branch", "--list", "feat/*"], R),
    (["stash", "list"], R), (["tag"], R), (["tag", "-l"], R),
    (["remote", "-v"], R), (["remote", "show", "origin"], R), (["reflog"], R),
    # mutate
    (["pull", "--ff-only"], M), (["commit", "-m", "x"], M), (["commit", "--amend"], M),
    (["add", "a.txt"], M), (["switch", "main"], M), (["switch", "-c", "x"], M),
    (["checkout", "-b", "x"], M), (["checkout", "main"], M), (["merge", "dev"], M),
    (["push", "origin", "main"], M), (["push", "-u", "origin", "main"], M),
    (["stash"], M), (["stash", "pop"], M), (["stash", "-m", "msg"], M),
    (["branch", "feat"], M), (["branch", "-d", "feat"], M),
    (["restore", "--staged", "a.txt"], M), (["reset", "--soft", "HEAD~1"], M),
    (["reset", "HEAD", "a.txt"], M), (["tag", "v1"], M),
    (["remote", "add", "x", "url"], M),
    # destructive
    (["push", "--force"], D), (["push", "-f"], D), (["push", "-uf"], D),
    (["push", "--force-with-lease", "origin", "main"], D),
    (["push", "origin", "+main"], D), (["push", "origin", ":old"], D),
    (["push", "--delete", "origin", "x"], D),
    (["reset", "--hard"], D), (["clean", "-fd"], D), (["rebase", "main"], D),
    (["pull", "--rebase"], D), (["branch", "-D", "x"], D),
    (["checkout", "--", "a.txt"], D), (["checkout", "."], D), (["checkout", "-f", "x"], D),
    (["switch", "--discard-changes", "x"], D), (["switch", "-f", "x"], D),
    (["restore", "a.txt"], D), (["stash", "drop"], D), (["stash", "clear"], D),
    (["tag", "-d", "v1"], D), (["remote", "remove", "x"], D),
    (["reflog", "expire"], D), (["gc"], D), (["filter-branch"], D),
    # unknown / unparseable default to destructive
    ([], D), (["-C", "x"], D), (["totally-unknown"], D),
])
def test_classify(args, tier):
    assert classify(args) is tier
