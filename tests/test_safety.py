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


@pytest.mark.parametrize("args,tier", [
    # long-option abbreviations (git accepts any unambiguous prefix)
    (["reset", "--ha"], D), (["reset", "--hard"], D),
    (["push", "--force-with"], D), (["push", "--force-w", "origin", "main"], D),
    (["push", "--dele", "origin", "x"], D), (["push", "--mirr"], D),
    (["pull", "--reb"], D), (["switch", "--disc", "x"], D),
    (["switch", "--force-c", "y"], D), (["checkout", "--for", "x"], D),
    (["tag", "--forc", "v1"], D), (["tag", "--delet", "v1"], D),
    (["restore", "--staged", "--work", "a.txt"], D),
    # abbreviations must never lower severity
    (["restore", "--stag", "a.txt"], D), (["branch", "--lis"], R),
    # checkout of paths without `--`
    (["checkout", "a.txt"], D), (["checkout", "HEAD", "a.txt"], D),
    (["checkout", "src/"], D), (["checkout", "./"], D), (["checkout", "*"], D),
    (["checkout", ":/"], D), (["checkout", "--ours", "a.txt"], D),
    (["checkout", "-p"], D), (["checkout"], D),
    (["checkout", "main"], M), (["checkout", "-b", "x"], M),
    (["checkout", "-b", "x", "origin/main"], M), (["checkout", "-c", "x"], M),
    # documented accepted false positive: slashed branch names need confirmation
    (["checkout", "feature/x"], D),
    # fetch is conditional
    (["fetch"], R), (["fetch", "--all"], R), (["fetch", "origin"], R),
    (["fetch", "origin", "main"], R),
    (["fetch", "origin", "+main:main"], D), (["fetch", "origin", "main:main"], D),
    (["fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*"], D),
    (["fetch", "-f"], D), (["fetch", "--force"], D), (["fetch", "-p"], D),
    (["fetch", "--prune"], D), (["fetch", "--prune-tags"], D),
    (["fetch", "--update-head-ok"], D), (["fetch", "-u"], D),
    (["fetch", "--upload-pack=evil", "origin"], D), (["fetch", "--refmap=x"], D),
    (["fetch", "--prun"], D), (["fetch", "--upload", "x", "origin"], D),
    (["pull", "origin", "+a:b"], D), (["pull", "origin", "a:b"], D),
    (["pull", "origin", "main"], M),
    # global options allowlist
    (["-c", "core.fsmonitor=CMD", "status"], D),
    (["-c", "core.sshCommand=CMD", "fetch"], D),
    (["-c", "pull.rebase=true", "pull"], D),
    (["--exec-path=/evil", "fetch"], D),
    (["--attr-source", "status", "push", "--force"], D),
    (["--config-env=a=B", "status"], D), (["--namespace=x", "status"], D),
    (["-C", "x", "diff"], R), (["--no-pager", "log"], R),
    (["--git-dir=.git", "status"], R), (["--git-dir", ".git", "status"], R),
    (["--work-tree=.", "status"], R), (["--work-tree", ".", "status"], R),
    (["--no-optional-locks", "status"], R), (["--no-replace-objects", "log"], R),
    (["--literal-pathspecs", "status"], R),
    (["-C", "x", "--no-pager", "push", "--force"], D),
    # minor hardening
    (["branch", "-C", "a", "b"], D),
    (["merge", "--abort"], D), (["merge", "--abo"], D),
    (["cherry-pick", "--abort"], D), (["revert", "--abort"], D),
    (["merge", "dev"], M), (["cherry-pick", "abc"], M), (["revert", "abc"], M),
])
def test_classify_bypass_attempts(args, tier):
    assert classify(args) is tier


@pytest.mark.parametrize("args,tier", [
    # command-execution options
    (["ls-remote", "--upload-pack=evil", "."], D),
    (["ls-remote", "--upload-pack", "evil", "."], D),
    (["ls-remote", "--uplo=evil", "."], D),
    (["ls-remote", "--exec=evil", "."], D),
    (["push", "--receive-pack=evil", "origin"], D),
    (["push", "--recei=evil", "origin"], D),
    (["push", "--exec=evil", "origin"], D),
    (["pull", "--upload-pack=evil"], D),
    (["pull", "--receive-pack=evil"], D),
    (["pull", "--exec=evil"], D),
    (["fetch", "--receive-pack=evil", "origin"], D),
    (["fetch", "--exec=evil", "origin"], D),
    (["clone", "--upload-pack=evil", "url"], D),
    (["clone", "--uplo=evil", "url"], D),
    (["clone", "--receive-pack=evil", "url"], D),
    (["clone", "--exec=evil", "url"], D),
    (["clone", "-u", "evil", "url"], D),
    (["clone", "-c", "core.sshCommand=evil", "url"], D),
    (["clone", "--config", "core.sshCommand=evil", "url"], D),
    (["clone", "--conf=core.sshCommand=evil", "url"], D),
    (["clone", "--template=/evil", "url"], D),
    (["clone", "--templ=/evil", "url"], D),
    # grep pager
    (["grep", "-O", "x"], D), (["grep", "-Osh", "x"], D), (["grep", "-nOe", "x"], D),
    (["grep", "--open-files-in-pager=evil", "x"], D),
    (["grep", "--open-files", "x"], D),
    # file-writing options on read commands
    (["diff", "--output=a.txt"], D), (["diff", "--outp=a.txt"], D),
    (["diff", "--output", "a.txt"], D),
    (["log", "--output=a.txt"], D), (["log", "--outp=a.txt"], D),
    (["show", "--output=a.txt"], D), (["show", "--outp=a.txt", "HEAD"], D),
    # pull parity with fetch
    (["pull", "-f"], D), (["pull", "--force"], D), (["pull", "--forc"], D),
    (["pull", "--prune"], D), (["pull", "--prun"], D), (["pull", "-p"], D),
    # remote update
    (["remote", "update", "--prune"], D), (["remote", "update", "--prun"], D),
    (["remote", "update", "-p"], D), (["remote", "-v", "update", "-p"], D),
    (["remote", "update"], R), (["remote", "update", "origin"], R),
    # must stay at their current tier
    (["ls-remote"], R), (["ls-remote", "origin"], R),
    (["clone", "url", "dir"], M), (["clone", "--depth", "1", "url"], M),
    (["grep", "-n", "foo"], R), (["grep", "--only-matching", "foo"], R),
    (["diff", "HEAD~1"], R), (["diff", "--stat"], R),
    (["log", "--oneline"], R), (["show", "HEAD"], R),
    (["pull", "--ff-only"], M), (["push", "origin", "main"], M),
])
def test_classify_exec_and_write_options(args, tier):
    assert classify(args) is tier
