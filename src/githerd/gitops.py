from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Diverged, Failed, NetworkError, Ok, Outcome, UpToDate,
)
from githerd.repos import snapshot
from githerd.runner import GitError, ProgressCb, git_env, run_git
from githerd.textsafe import clean_message

log = logging.getLogger("githerd.gitops")

_AUTH_MARKERS = (
    "authentication failed", "could not read username", "could not read password",
    "terminal prompts disabled", "permission denied (publickey",
    "requested url returned error: 401", "requested url returned error: 403",
)
_NETWORK_MARKERS = (
    "could not resolve host", "unable to access", "connection timed out",
    "connection refused", "network is unreachable",
    "could not read from remote repository",
)


def _tail(text: str, lines: int = 3) -> str:
    kept = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return clean_message(" | ".join(kept[-lines:]))


_SIMPLE_ESCAPES = {
    "\\": "\\", '"': '"', "t": "\t", "n": "\n", "r": "\r",
    "a": "\a", "b": "\b", "f": "\f", "v": "\v",
}


def _unquote_git_path(text: str) -> str:
    """Undo git's C-style path quoting (the UTF-8 bytes ``\\303\\257`` inside quotes become one character).

    Text that is not wrapped in double quotes is returned unchanged.
    """
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body = text[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in "01234567":  # octal byte escape, up to three digits
            j = i + 1
            while j < len(body) and j < i + 4 and body[j] in "01234567":
                j += 1
            out.append(int(body[i + 1:j], 8) & 0xFF)
            i = j
        elif nxt in _SIMPLE_ESCAPES:
            out += _SIMPLE_ESCAPES[nxt].encode("utf-8")
            i += 2
        else:  # unknown escape: keep it verbatim
            out += ("\\" + nxt).encode("utf-8")
            i += 2
    return out.decode("utf-8", errors="replace")


def parse_blocking_files(stderr: str) -> list[str]:
    """Paths git lists under its 'would be overwritten' error messages."""
    files: list[str] = []
    capturing = False
    for line in stderr.splitlines():
        if "would be overwritten" in line.lower():
            capturing = True
            continue
        if capturing:
            if line.startswith(("\t", " ")) and line.strip():
                files.append(_unquote_git_path(line.strip()))
            else:
                capturing = False
    return files


def classify_failure(stderr: str, remote: str = "") -> Outcome:
    low = stderr.lower()
    if any(m in low for m in _AUTH_MARKERS):  # checked first: ssh auth also says "could not read"
        return AuthRequired(remote=remote)
    if any(m in low for m in _NETWORK_MARKERS):
        return NetworkError(message=_tail(stderr))
    return Failed(message=_tail(stderr) or "git failed")


def redact_url(url: str) -> str:
    """Strip any ``user[:password]@`` userinfo from a ``scheme://`` URL."""
    if "://" not in url:
        return url  # scp-style (git@host:path) and local paths carry no secret
    try:
        parts = urlsplit(url)
        if "@" not in parts.netloc:
            # An '@' elsewhere means userinfo containing an unescaped '/', '?' or '#'
            # ended the netloc early: ambiguous, so fail closed.
            return "" if "@" in url else url
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    except ValueError:
        return ""  # unparseable: safer to show nothing than risk leaking


async def _remote_url(repo: Path, upstream: str | None) -> str:
    name = upstream.split("/", 1)[0] if upstream else "origin"
    res = await run_git(repo, "config", "--get", f"remote.{name}.url")
    return redact_url(res.stdout.strip()) if res.ok else ""


async def _rev(repo: Path, ref: str) -> str:
    res = await run_git(repo, "rev-parse", "--verify", "--quiet", ref)
    return res.stdout.strip() if res.ok else ""


_HEADS = "refs/heads/"
HEAD_AND_BRANCH_ARGS = ("rev-parse", "HEAD", "--symbolic-full-name", "HEAD")


def parse_head_and_branch(stdout: str) -> tuple[str, str | None] | None:
    """Parse ``git rev-parse HEAD --symbolic-full-name HEAD``: the sha, then the full ref.

    The full ref (``refs/heads/<name>``) is unambiguous even when a tag has the branch's
    name (``--abbrev-ref`` would print ``heads/<name>`` then). A detached HEAD prints
    ``HEAD`` and yields branch ``None``. Anything else (a non-branch ref, missing lines)
    is unparseable and gives ``None``: a branch name is never guessed.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    sha, ref = lines
    if ref == "HEAD":
        return sha, None
    if ref.startswith(_HEADS) and len(ref) > len(_HEADS):
        return sha, ref[len(_HEADS):]
    return None


async def head_and_branch(repo: Path) -> tuple[str, str | None] | None:
    """``(head sha, branch or None if detached)`` in ONE git spawn; ``None`` when unreadable.

    An unborn repo (no commits yet) or a path that is not a repo is unreadable.
    """
    res = await run_git(repo, *HEAD_AND_BRANCH_ARGS)
    return parse_head_and_branch(res.stdout) if res.ok else None


def git_sync(repo: Path | str, *args: str, timeout: float = 10) -> str | None:
    """Run ``git -C repo args`` synchronously: stripped stdout, or ``None`` on any failure.

    Needs no event loop, so it is safe in a ``finally`` that runs during cancellation.
    Never raises.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=git_env(),
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # timeout, git missing, OS error: never fatal to the caller
        log.warning("git %s failed in %s: %r", " ".join(args), repo, exc)
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def head_sync(repo: Path | str) -> str | None:
    """Current HEAD sha read synchronously, or ``None`` when it cannot be read."""
    return git_sync(repo, "rev-parse", "HEAD")


def head_and_branch_sync(repo: Path | str) -> tuple[str, str | None] | None:
    """Synchronous twin of ``head_and_branch`` (same single spawn, same parser)."""
    out = git_sync(repo, *HEAD_AND_BRANCH_ARGS)
    return parse_head_and_branch(out) if out else None


async def pull(repo: Path, on_progress: ProgressCb | None = None) -> Outcome:
    try:
        return await _pull(repo, on_progress)
    except GitError as exc:
        return Failed(message=clean_message(str(exc)))


async def _pull(repo: Path, on_progress: ProgressCb | None) -> Outcome:
    snap = await snapshot(repo)
    if snap.branch is None:
        return Failed(message="detached HEAD: switch to a branch before pulling", retryable=False)
    if snap.upstream is None:
        return Failed(
            message=clean_message(f"branch '{snap.branch}' has no upstream configured"),
            retryable=False,
        )
    before = snap.head
    res = await run_git(repo, "pull", "--ff-only", "--progress", on_progress=on_progress)
    if not res.ok:
        low = res.stderr.lower()
        if "would be overwritten" in low:
            return BlockedDirty(files=snap.dirty, blocking=parse_blocking_files(res.stderr))
        if "not possible to fast-forward" in low:
            again = await snapshot(repo)  # pull already fetched, counts are current
            return Diverged(ahead=again.ahead, behind=again.behind)
        return classify_failure(res.stderr, await _remote_url(repo, snap.upstream))
    after = await _rev(repo, "HEAD")
    if after == before:
        return UpToDate()
    count = await run_git(repo, "rev-list", "--count", f"{before}..{after}")
    names = await run_git(repo, "diff", "--name-only", before, after)
    return Ok(
        commits=int(count.stdout.strip() or 0),
        files=len([ln for ln in names.stdout.splitlines() if ln.strip()]),
        before_head=before,
        after_head=after,
        branch=snap.branch,
    )


async def fetch(repo: Path, on_progress: ProgressCb | None = None) -> Outcome:
    try:
        return await _fetch(repo, on_progress)
    except GitError as exc:
        return Failed(message=clean_message(str(exc)))


async def _fetch(repo: Path, on_progress: ProgressCb | None) -> Outcome:
    snap = await snapshot(repo)
    before = await _rev(repo, "@{u}")
    res = await run_git(repo, "fetch", "--progress", on_progress=on_progress)
    if not res.ok:
        return classify_failure(res.stderr, await _remote_url(repo, snap.upstream))
    after = await _rev(repo, "@{u}")
    if before == after:
        return UpToDate()
    now = await snapshot(repo)
    return Ok(commits=now.behind, files=0, before_head=snap.head, after_head=snap.head)
