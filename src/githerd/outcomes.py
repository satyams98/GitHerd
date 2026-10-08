from __future__ import annotations

from collections import Counter
from typing import Annotated, Iterable, Literal

from pydantic import BaseModel, Field

from githerd.textsafe import clean_message


class FileChange(BaseModel):
    status: str  # two-character code, e.g. " M", "A ", "??", "UU"
    path: str


class Ok(BaseModel):
    kind: Literal["ok"] = "ok"
    commits: int
    files: int
    before_head: str
    after_head: str
    branch: str | None = None  # branch that was pulled; None when unknown or detached


class UpToDate(BaseModel):
    kind: Literal["up_to_date"] = "up_to_date"


class BlockedDirty(BaseModel):
    kind: Literal["blocked_dirty"] = "blocked_dirty"
    files: list[FileChange]
    blocking: list[str] = Field(default_factory=list)  # paths git said would be overwritten


class Diverged(BaseModel):
    kind: Literal["diverged"] = "diverged"
    ahead: int
    behind: int


class Conflict(BaseModel):
    kind: Literal["conflict"] = "conflict"
    files: list[str]
    stash_kept: bool = False  # a stash-and-pull pop conflicted, so git kept the stash


class AuthRequired(BaseModel):
    kind: Literal["auth_required"] = "auth_required"
    remote: str


class NetworkError(BaseModel):
    kind: Literal["network_error"] = "network_error"
    message: str


class Failed(BaseModel):
    kind: Literal["failed"] = "failed"
    message: str
    retryable: bool = True  # False for deterministic failures that retrying cannot fix


Outcome = Annotated[
    Ok | UpToDate | BlockedDirty | Diverged | Conflict
    | AuthRequired | NetworkError | Failed,
    Field(discriminator="kind"),
]

_LABELS = {
    "ok": "updated",
    "up_to_date": "up to date",
    "blocked_dirty": "blocked by local changes",
    "diverged": "diverged",
    "conflict": "conflicting",
    "auth_required": "need credentials",
    "network_error": "network error",
    "failed": "failed",
}


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def describe(outcome: "Outcome") -> str:
    if isinstance(outcome, Ok):
        text = f"updated: {_n(outcome.commits, 'commit')}"
        if outcome.files:
            text += f", {_n(outcome.files, 'file')}"
        return text
    if isinstance(outcome, UpToDate):
        return "already up to date"
    if isinstance(outcome, BlockedDirty):
        return f"blocked: {_n(len(outcome.files), 'local change')}"
    if isinstance(outcome, Diverged):
        return f"diverged: {outcome.ahead} ahead, {outcome.behind} behind"
    if isinstance(outcome, Conflict):
        return f"conflict in {_n(len(outcome.files), 'file')}"
    if isinstance(outcome, AuthRequired):
        return "needs credentials"
    if isinstance(outcome, NetworkError):
        return f"network error: {clean_message(outcome.message)}"
    return f"failed: {clean_message(outcome.message)}"


def summarize(outcomes: Iterable["Outcome"]) -> str:
    counts = Counter(o.kind for o in outcomes)
    return ", ".join(
        f"{counts[kind]} {label}" for kind, label in _LABELS.items() if counts[kind]
    )
