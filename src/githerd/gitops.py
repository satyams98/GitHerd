from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Diverged, Failed, NetworkError, Ok, Outcome, UpToDate,
)
from githerd.repos import snapshot
from githerd.runner import GitError, ProgressCb, run_git

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
    return " | ".join(kept[-lines:])


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


async def pull(repo: Path, on_progress: ProgressCb | None = None) -> Outcome:
    try:
        return await _pull(repo, on_progress)
    except GitError as exc:
        return Failed(message=str(exc))


async def _pull(repo: Path, on_progress: ProgressCb | None) -> Outcome:
    snap = await snapshot(repo)
    if snap.branch is None:
        return Failed(message="detached HEAD: switch to a branch before pulling")
    if snap.upstream is None:
        return Failed(message=f"branch '{snap.branch}' has no upstream configured")
    before = snap.head
    res = await run_git(repo, "pull", "--ff-only", "--progress", on_progress=on_progress)
    if not res.ok:
        low = res.stderr.lower()
        if "would be overwritten" in low:
            return BlockedDirty(files=snap.dirty)
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
    )


async def fetch(repo: Path, on_progress: ProgressCb | None = None) -> Outcome:
    try:
        return await _fetch(repo, on_progress)
    except GitError as exc:
        return Failed(message=str(exc))


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
