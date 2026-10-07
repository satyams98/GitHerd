from __future__ import annotations

import re
from enum import Enum
from typing import Callable, Sequence


class Tier(str, Enum):
    READ = "read"
    MUTATE = "mutate"
    DESTRUCTIVE = "destructive"


READ, MUTATE, DESTRUCTIVE = Tier.READ, Tier.MUTATE, Tier.DESTRUCTIVE

# Global options accepted before the subcommand. Anything else (notably -c,
# --config-env, --exec-path, --attr-source, --namespace) can change behaviour or
# run commands, so it makes classify() fall back to DESTRUCTIVE.
_GLOBAL_WITH_VALUE = {"-C", "--git-dir", "--work-tree"}
_GLOBAL_INLINE_VALUE = ("--git-dir=", "--work-tree=")
_GLOBAL_FLAGS = {
    "--no-pager", "--no-optional-locks", "--no-replace-objects", "--literal-pathspecs",
}

_READ_SUBCOMMANDS = {
    "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
    "ls-files", "ls-tree", "ls-remote", "describe", "shortlog", "grep", "cat-file",
    "name-rev", "merge-base", "show-ref", "for-each-ref", "count-objects",
}
_MUTATE_SUBCOMMANDS = {"add", "commit", "init", "clone"}

# A bare ref name: no path separators, dots, globs or colons.
_PLAIN_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")


def _split(args: Sequence[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _GLOBAL_WITH_VALUE:
            i += 2
        elif arg in _GLOBAL_FLAGS or arg.startswith(_GLOBAL_INLINE_VALUE):
            i += 1
        elif arg.startswith("-"):
            raise IndexError("unsupported global option")
        else:
            return arg, list(args[i + 1:])
    raise IndexError("no subcommand")


def _long(flags: set[str], *names: str) -> bool:
    """Exact match (or `--name=value`). Use where a match lowers severity."""
    return any(
        f == n or f.startswith(n + "=")
        for f in flags if f.startswith("--") for n in names
    )


def _abbr(flags: set[str], *names: str) -> bool:
    """Match any abbreviation git would accept. Use where a match raises severity."""
    return any(
        len(key) >= 3 and n.startswith(key)
        for f in flags if f.startswith("--")
        for key in (f.split("=", 1)[0],)
        for n in names
    )


def _short(flags: set[str], chars: str) -> bool:
    return any(
        c in chars
        for f in flags if f.startswith("-") and not f.startswith("--") for c in f[1:]
    )


def _is_refspec(arg: str) -> bool:
    return arg.startswith("+") or ":" in arg


# Options that make git run an arbitrary program (transport helpers).
_EXEC_OPTS = ("--upload-pack", "--receive-pack", "--exec")


Handler = Callable[[set[str], list[str], list[str]], Tier]


def _push(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if (
        _abbr(flags, "--force", "--force-with-lease", "--force-if-includes",
              "--mirror", "--delete", "--prune", *_EXEC_OPTS)
        or _short(flags, "fd")
        or any(p.startswith(("+", ":")) for p in positional)
    ):
        return DESTRUCTIVE
    return MUTATE


def _pull(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if (
        _abbr(flags, "--rebase", "--force", "--prune", *_EXEC_OPTS)
        or _short(flags, "rfp")
        or any(_is_refspec(p) for p in positional)
    ):
        return DESTRUCTIVE
    return MUTATE


def _fetch(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if (
        _abbr(flags, "--force", "--prune", "--prune-tags", "--update-head-ok",
              "--refmap", *_EXEC_OPTS)
        or _short(flags, "fpuP")
        or any(_is_refspec(p) for p in positional)
    ):
        return DESTRUCTIVE
    return READ


def _ls_remote(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    return DESTRUCTIVE if _abbr(flags, *_EXEC_OPTS) else READ


def _clone(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if (
        _abbr(flags, "--config", "--template", *_EXEC_OPTS)
        or _short(flags, "uc")
    ):
        return DESTRUCTIVE
    return MUTATE


def _grep(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if _abbr(flags, "--open-files-in-pager") or _short(flags, "O"):
        return DESTRUCTIVE
    return READ


def _writes_output(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    """diff/log/show: `--output=<file>` writes (clobbers) a file."""
    return DESTRUCTIVE if _abbr(flags, "--output") else READ


def _abortable(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    return DESTRUCTIVE if _abbr(flags, "--abort") else MUTATE


def _branch(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if _short(flags, "DMfC") or _abbr(flags, "--force"):
        return DESTRUCTIVE
    if _short(flags, "dmcu") or _long(
        flags, "--delete", "--move", "--copy", "--set-upstream-to", "--unset-upstream"
    ):
        return MUTATE
    if (
        _short(flags, "larv")
        or _long(flags, "--list", "--all", "--remotes", "--show-current", "--merged",
                 "--no-merged", "--contains", "--no-contains", "--verbose")
        or not positional
    ):
        return READ
    return MUTATE


def _tag(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if _short(flags, "df") or _abbr(flags, "--delete", "--force"):
        return DESTRUCTIVE
    if _short(flags, "ln") or _long(flags, "--list") or not positional:
        return READ
    return MUTATE


def _stash(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    sub = rest[0] if rest and not rest[0].startswith("-") else "push"
    if sub in {"list", "show"}:
        return READ
    if sub in {"drop", "clear"}:
        return DESTRUCTIVE
    if sub in {"push", "save", "pop", "apply", "branch", "create", "store"}:
        return MUTATE
    return DESTRUCTIVE


def _remote(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if positional and positional[0] == "update":
        return DESTRUCTIVE if _abbr(flags, "--prune") or _short(flags, "p") else READ
    if not positional or positional[0] in {"show", "get-url"}:
        return READ
    if positional[0] in {"add", "rename", "set-url", "set-head", "set-branches"}:
        return MUTATE
    return DESTRUCTIVE


def _reflog(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if not positional or positional[0] == "show":
        return READ
    return DESTRUCTIVE


def _reset(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    return DESTRUCTIVE if _abbr(flags, "--hard", "--merge") else MUTATE


def _restore(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    staged_only = (_long(flags, "--staged") or _short(flags, "S")) and not (
        _abbr(flags, "--worktree") or _short(flags, "W")
    )
    return MUTATE if staged_only else DESTRUCTIVE


def _checkout(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if (
        _short(flags, "fB")
        or _abbr(flags, "--force", "--discard-changes")
        or "--" in rest
        or "." in positional
    ):
        return DESTRUCTIVE
    # KNOWN RESIDUAL false negative: without `--`, `checkout <plain-ref>` is
    # ambiguous with a file restore (e.g. `checkout Makefile` discards local
    # changes to Makefile) and is classified MUTATE, because `checkout main` must
    # stay MUTATE. The real mitigation is that githerd's tool layer only emits
    # `switch` for branch changes, never `checkout <ref>`. Everything else
    # (paths, globs, dots, slashes, extra args) needs confirmation.
    if not flags and len(positional) == 1 and _PLAIN_REF.fullmatch(positional[0]):
        return MUTATE
    if flags and flags <= {"-b", "-c"} and 1 <= len(positional) <= 2:
        return MUTATE
    return DESTRUCTIVE


def _switch(flags: set[str], positional: list[str], rest: list[str]) -> Tier:
    if _short(flags, "fC") or _abbr(flags, "--force", "--discard-changes", "--force-create"):
        return DESTRUCTIVE
    return MUTATE


_HANDLERS: dict[str, Handler] = {
    "push": _push, "pull": _pull, "fetch": _fetch, "branch": _branch, "tag": _tag,
    "stash": _stash, "remote": _remote, "reflog": _reflog, "reset": _reset,
    "restore": _restore, "checkout": _checkout, "switch": _switch,
    "ls-remote": _ls_remote, "clone": _clone, "grep": _grep,
    "diff": _writes_output, "log": _writes_output, "show": _writes_output,
    "merge": _abortable, "cherry-pick": _abortable, "revert": _abortable,
}


def classify(args: Sequence[str]) -> Tier:
    try:
        sub, rest = _split(args)
    except IndexError:
        return DESTRUCTIVE
    flags = {a for a in rest if a.startswith("-") and a != "--"}
    positional = [a for a in rest if not a.startswith("-")]
    handler = _HANDLERS.get(sub)
    if handler:
        return handler(flags, positional, rest)
    if sub in _READ_SUBCOMMANDS:
        return READ
    if sub in _MUTATE_SUBCOMMANDS:
        return MUTATE
    return DESTRUCTIVE
