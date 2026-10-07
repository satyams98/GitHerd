from __future__ import annotations

from enum import Enum
from typing import Callable, Sequence


class Tier(str, Enum):
    READ = "read"
    MUTATE = "mutate"
    DESTRUCTIVE = "destructive"


READ, MUTATE, DESTRUCTIVE = Tier.READ, Tier.MUTATE, Tier.DESTRUCTIVE

_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

_READ_SUBCOMMANDS = {
    "status", "log", "diff", "show", "blame", "fetch", "rev-parse", "rev-list",
    "ls-files", "ls-tree", "ls-remote", "describe", "shortlog", "grep", "cat-file",
    "name-rev", "merge-base", "show-ref", "for-each-ref", "count-objects",
}
_MUTATE_SUBCOMMANDS = {
    "add", "commit", "merge", "cherry-pick", "revert", "init", "clone",
}


def _split(args: Sequence[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _GLOBAL_WITH_VALUE:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg, list(args[i + 1:])
    raise IndexError("no subcommand")


def _long(flags: set[str], *names: str) -> bool:
    return any(
        f == n or f.startswith(n + "=")
        for f in flags if f.startswith("--") for n in names
    )


def _short(flags: set[str], chars: str) -> bool:
    return any(
        c in chars
        for f in flags if f.startswith("-") and not f.startswith("--") for c in f[1:]
    )


Handler = Callable[[set[str], list[str], list[str]], Tier]


def _push(flags, positional, rest):
    if (
        _long(flags, "--force", "--force-with-lease", "--force-if-includes",
              "--mirror", "--delete", "--prune")
        or _short(flags, "fd")
        or any(p.startswith(("+", ":")) for p in positional)
    ):
        return DESTRUCTIVE
    return MUTATE


def _pull(flags, positional, rest):
    if _long(flags, "--rebase") or _short(flags, "r"):
        return DESTRUCTIVE
    return MUTATE


def _branch(flags, positional, rest):
    if _short(flags, "DMf") or _long(flags, "--force"):
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


def _tag(flags, positional, rest):
    if _short(flags, "df") or _long(flags, "--delete", "--force"):
        return DESTRUCTIVE
    if _short(flags, "ln") or _long(flags, "--list") or not positional:
        return READ
    return MUTATE


def _stash(flags, positional, rest):
    sub = rest[0] if rest and not rest[0].startswith("-") else "push"
    if sub in {"list", "show"}:
        return READ
    if sub in {"drop", "clear"}:
        return DESTRUCTIVE
    if sub in {"push", "save", "pop", "apply", "branch", "create", "store"}:
        return MUTATE
    return DESTRUCTIVE


def _remote(flags, positional, rest):
    if not positional or positional[0] in {"show", "get-url", "update"}:
        return READ
    if positional[0] in {"add", "rename", "set-url", "set-head", "set-branches"}:
        return MUTATE
    return DESTRUCTIVE


def _reflog(flags, positional, rest):
    if not positional or positional[0] == "show":
        return READ
    return DESTRUCTIVE


def _reset(flags, positional, rest):
    return DESTRUCTIVE if _long(flags, "--hard", "--merge") else MUTATE


def _restore(flags, positional, rest):
    staged_only = (_long(flags, "--staged") or _short(flags, "S")) and not (
        _long(flags, "--worktree") or _short(flags, "W")
    )
    return MUTATE if staged_only else DESTRUCTIVE


def _checkout(flags, positional, rest):
    if (
        _short(flags, "fB")
        or _long(flags, "--force", "--discard-changes")
        or "--" in rest
        or "." in positional
    ):
        return DESTRUCTIVE
    return MUTATE


def _switch(flags, positional, rest):
    if _short(flags, "fC") or _long(flags, "--force", "--discard-changes", "--force-create"):
        return DESTRUCTIVE
    return MUTATE


_HANDLERS: dict[str, Handler] = {
    "push": _push, "pull": _pull, "branch": _branch, "tag": _tag, "stash": _stash,
    "remote": _remote, "reflog": _reflog, "reset": _reset, "restore": _restore,
    "checkout": _checkout, "switch": _switch,
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
