# githerd Plan 1: Core Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the UI-free, LLM-free engine of githerd: repo discovery, typed git outcomes, parallel pull/fetch with progress events, safety classification, an undo journal, and a minimal plain-text CLI (`status`, `pull`, `undo`) that proves it end to end.

**Architecture:** An async git runner (`runner`) feeds per-operation functions (`gitops`) that return a typed `Outcome` union (`outcomes`). A bounded-concurrency bulk runner (`bulk`) fans operations across repos and emits progress events; mutating bulk operations write to a journal (`journal`) that `undo` reverses. `safety` classifies any git argument list into read/mutate/destructive. Everything is tested against real temporary git repos with local bare remotes.

**Tech Stack:** Python 3.12 (>=3.11), asyncio, pydantic v2, typer, pytest, pytest-asyncio. Real `git` 2.x on Windows.

## Roadmap (this plan is 1 of 4)

| Plan | Delivers | Depends on |
|---|---|---|
| **1. Core engine (this plan)** | discovery, outcomes, runner, gitops (pull/fetch), safety, journal/undo, bulk runner, plain CLI | none |
| 2. UI | inline-flow renderer spike (Textual inline vs rich + prompt_toolkit), live dashboard, cards, diff viewer, theme | Plan 1 |
| 3. LLM + agent + config | `llm` layer (OpenAI + Anthropic adapters, presets incl. Ollama), tool registry, agent loop, first-run wizard, keyring | Plans 1, 2 |
| 4. Remaining git features | branching, stage/commit/amend + AI commit drafts, stash, log/blame/search, conflict card, dry-run, session log, follow-up memory | Plans 1-3 |

## Global Constraints

- Windows only; Python `>=3.11`; development interpreter is `C:\Users\ssingh49\AppData\Local\Programs\Python\Python312\python.exe` (the bare `python` on PATH is the Microsoft Store stub, do not use it).
- Git always runs non-interactively: `GIT_TERMINAL_PROMPT=0` (spec §5, auth).
- Every git failure becomes a typed `Outcome`, never an exception to the caller; one repo failing never stops others (spec §7).
- `safety.classify` defaults unknown or unparseable git arguments to `destructive` (spec §4).
- Every mutation records `before_head`/`after_head`; undo is journaled under `.githerd/` at the working-directory root, and that directory self-ignores (spec §6).
- Mutating commands the user asked for run immediately; this plan has no confirmation UI, so undo skips (never forces) a repo that moved since the operation. The confirmation flow arrives with Plan 2.
- Plan 1 console output is ASCII only (glyphs and colour arrive in Plan 2).
- Package `githerd`; entry points `githerd` and `gherd` (spec §2).
- Tests never touch the user's git config: a session fixture points `GIT_CONFIG_GLOBAL` at a temp file and sets `GIT_CONFIG_NOSYSTEM=1`.

## File Structure

```
pyproject.toml
.gitignore
src/githerd/__init__.py        version only
src/githerd/outcomes.py        Outcome union, describe(), summarize()
src/githerd/runner.py          run_git(), GitResult, GitError, parse_progress()
src/githerd/repos.py           discover_repos(), RepoSnapshot, snapshot(), snapshot_all()
src/githerd/safety.py          Tier, classify()
src/githerd/gitops.py          pull(), fetch(), classify_failure()
src/githerd/journal.py         JournalEntry, OpSet, Journal
src/githerd/undo.py            UndoItem, undo_last()
src/githerd/bulk.py            RepoEvent, run_bulk(), pull_repos()
src/githerd/cli.py             typer app: status, pull, undo
tests/conftest.py              git env isolation, make_repo / push_upstream / commit_local / git fixtures
tests/test_*.py                one file per module
```

---

### Task 1: Project scaffold and test harness

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/githerd/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces (fixtures used by every later test): `git(cwd, *args) -> str` (runs real git, returns stripped stdout, raises on failure); `make_repo(name, parent=None) -> Path` (creates bare remote plus a clone with one pushed commit, upstream set to `origin/main`, default parent `tmp_path/"work"`); `push_upstream(repo, filename, content="x\n", message="upstream change")` (commits to the repo's remote from a second clone); `commit_local(repo, filename, content="y\n", message="local change")`.

- [ ] **Step 1: Create the virtual environment**

Run (PowerShell, from `C:\Users\ssingh49\Projects\CLI`):
```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
```
Expected: `.venv\Scripts\python.exe` exists.

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "githerd"
version = "0.1.0"
description = "Herd all your git repos with plain English."
requires-python = ">=3.11"
dependencies = ["pydantic>=2.6", "typer>=0.12"]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[project.scripts]
githerd = "githerd.cli:app"
gherd = "githerd.cli:app"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Write `.gitignore`, package files**

`.gitignore`:
```
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
.githerd/
```

`src/githerd/__init__.py`:
```python
__version__ = "0.1.0"
```

`tests/__init__.py`: empty file.

- [ ] **Step 4: Write `tests/conftest.py`**

```python
import subprocess
from pathlib import Path

import pytest


def _run(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture(scope="session", autouse=True)
def isolated_git(tmp_path_factory):
    """Keep tests independent of the developer's global/system git config."""
    cfg = tmp_path_factory.mktemp("gitcfg") / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n",
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GIT_CONFIG_GLOBAL", str(cfg))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


@pytest.fixture
def git():
    return _run


@pytest.fixture
def make_repo(tmp_path):
    def make(name: str, parent: Path | None = None) -> Path:
        remote = tmp_path / "remotes" / f"{name}.git"
        remote.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(remote)],
            check=True, capture_output=True,
        )
        work = (parent or tmp_path / "work") / name
        work.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", str(remote), str(work)], check=True, capture_output=True
        )
        (work / "README.md").write_text("hello\n", encoding="utf-8")
        _run(work, "add", "README.md")
        _run(work, "commit", "-m", "initial")
        _run(work, "push", "-u", "origin", "main")
        return work

    return make


@pytest.fixture
def push_upstream(tmp_path):
    counter = {"n": 0}

    def push(repo: Path, filename: str, content: str = "x\n",
             message: str = "upstream change") -> None:
        remote = _run(repo, "remote", "get-url", "origin")
        counter["n"] += 1
        other = tmp_path / "other" / str(counter["n"])
        other.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", remote, str(other)], check=True, capture_output=True
        )
        (other / filename).write_text(content, encoding="utf-8")
        _run(other, "add", filename)
        _run(other, "commit", "-m", message)
        _run(other, "push", "origin", "main")

    return push


@pytest.fixture
def commit_local():
    def commit(repo: Path, filename: str, content: str = "y\n",
               message: str = "local change") -> None:
        (repo / filename).write_text(content, encoding="utf-8")
        _run(repo, "add", filename)
        _run(repo, "commit", "-m", message)

    return commit
```

- [ ] **Step 5: Write `tests/test_smoke.py`**

```python
import githerd


def test_version():
    assert githerd.__version__ == "0.1.0"


def test_make_repo_has_upstream(make_repo, git):
    repo = make_repo("alpha")
    assert git(repo, "rev-parse", "--abbrev-ref", "@{u}") == "origin/main"
```

- [ ] **Step 6: Install and run**

Run:
```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -v
```
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "chore: scaffold githerd package and test harness"
```

---

### Task 2: Typed outcomes

**Files:**
- Create: `src/githerd/outcomes.py`
- Test: `tests/test_outcomes.py`

**Interfaces:**
- Produces: models `FileChange(status: str, path: str)`, `Ok(commits, files, before_head, after_head)`, `UpToDate()`, `BlockedDirty(files: list[FileChange])`, `Diverged(ahead, behind)`, `Conflict(files: list[str])`, `AuthRequired(remote: str)`, `NetworkError(message: str)`, `Failed(message: str)`; union `Outcome` (discriminated on `kind`); `describe(outcome) -> str`; `summarize(outcomes: Iterable[Outcome]) -> str`.

- [ ] **Step 1: Write the failing test** (`tests/test_outcomes.py`)

```python
from pydantic import TypeAdapter

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Diverged, Failed, FileChange,
    NetworkError, Ok, Outcome, UpToDate, describe, summarize,
)


def test_outcome_roundtrip_uses_kind_discriminator():
    adapter = TypeAdapter(Outcome)
    original = BlockedDirty(files=[FileChange(status=" M", path="a.txt")])
    restored = adapter.validate_python(adapter.dump_python(original))
    assert restored == original
    assert restored.kind == "blocked_dirty"


def test_summarize_counts_in_fixed_order():
    outcomes = [
        Ok(commits=3, files=12, before_head="a", after_head="b"),
        UpToDate(),
        UpToDate(),
        BlockedDirty(files=[]),
    ]
    assert summarize(outcomes) == "1 updated, 2 up to date, 1 blocked by local changes"


def test_describe_each_outcome():
    assert describe(Ok(commits=3, files=12, before_head="a", after_head="b")) == "updated: 3 commits, 12 files"
    assert describe(Ok(commits=1, files=0, before_head="a", after_head="a")) == "updated: 1 commit"
    assert describe(UpToDate()) == "already up to date"
    assert describe(BlockedDirty(files=[FileChange(status="??", path="x")])) == "blocked: 1 local change"
    assert describe(Diverged(ahead=1, behind=2)) == "diverged: 1 ahead, 2 behind"
    assert describe(Conflict(files=["a", "b"])) == "conflict in 2 files"
    assert describe(AuthRequired(remote="origin")) == "needs credentials"
    assert describe(NetworkError(message="timed out")) == "network error: timed out"
    assert describe(Failed(message="boom")) == "failed: boom"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_outcomes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'githerd.outcomes'`.

- [ ] **Step 3: Implement** (`src/githerd/outcomes.py`)

```python
from __future__ import annotations

from collections import Counter
from typing import Annotated, Iterable, Literal

from pydantic import BaseModel, Field


class FileChange(BaseModel):
    status: str  # two-character code, e.g. " M", "A ", "??", "UU"
    path: str


class Ok(BaseModel):
    kind: Literal["ok"] = "ok"
    commits: int
    files: int
    before_head: str
    after_head: str


class UpToDate(BaseModel):
    kind: Literal["up_to_date"] = "up_to_date"


class BlockedDirty(BaseModel):
    kind: Literal["blocked_dirty"] = "blocked_dirty"
    files: list[FileChange]


class Diverged(BaseModel):
    kind: Literal["diverged"] = "diverged"
    ahead: int
    behind: int


class Conflict(BaseModel):
    kind: Literal["conflict"] = "conflict"
    files: list[str]


class AuthRequired(BaseModel):
    kind: Literal["auth_required"] = "auth_required"
    remote: str


class NetworkError(BaseModel):
    kind: Literal["network_error"] = "network_error"
    message: str


class Failed(BaseModel):
    kind: Literal["failed"] = "failed"
    message: str


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
        return f"network error: {outcome.message}"
    return f"failed: {outcome.message}"


def summarize(outcomes: Iterable["Outcome"]) -> str:
    counts = Counter(o.kind for o in outcomes)
    return ", ".join(
        f"{counts[kind]} {label}" for kind, label in _LABELS.items() if counts[kind]
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_outcomes.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/githerd/outcomes.py tests/test_outcomes.py
git commit -m "feat: typed outcome models with describe and summarize"
```

---

### Task 3: Async git runner and progress parsing

**Files:**
- Create: `src/githerd/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Produces: `ProgressCb = Callable[[str], None]`; `class GitError(Exception)`; `@dataclass(frozen=True) GitResult(code: int, stdout: str, stderr: str)` with property `ok`; `async run_git(repo: Path | str, *args: str, on_progress: ProgressCb | None = None) -> GitResult` (runs `git -C <repo> <args>`; never raises on non-zero exit; killing the process on cancellation); `parse_progress(line: str) -> tuple[str, int] | None`.

- [ ] **Step 1: Write the failing test** (`tests/test_runner.py`)

```python
import pytest

from githerd.runner import parse_progress, run_git


@pytest.mark.parametrize("line,expected", [
    ("Receiving objects:  78% (78/100)", ("Receiving objects", 78)),
    ("remote: Counting objects: 100% (5/5), done.", ("Counting objects", 100)),
    ("Resolving deltas:   5% (1/20)", ("Resolving deltas", 5)),
    ("Already up to date.", None),
    ("From /tmp/remote", None),
])
def test_parse_progress(line, expected):
    assert parse_progress(line) == expected


async def test_run_git_success(make_repo):
    repo = make_repo("a")
    result = await run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert result.ok
    assert result.stdout.strip() == "main"


async def test_run_git_failure_returns_result_not_exception(tmp_path):
    result = await run_git(tmp_path, "rev-parse", "HEAD")
    assert not result.ok
    assert result.code != 0
    assert result.stderr


async def test_run_git_streams_progress_lines(make_repo, tmp_path):
    repo = make_repo("a")
    remote = (await run_git(repo, "remote", "get-url", "origin")).stdout.strip()
    lines: list[str] = []
    result = await run_git(
        tmp_path, "clone", "--progress", remote, str(tmp_path / "dest"),
        on_progress=lines.append,
    )
    assert result.ok
    assert any("Cloning into" in line for line in lines)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'githerd.runner'`.

- [ ] **Step 3: Implement** (`src/githerd/runner.py`)

```python
from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ProgressCb = Callable[[str], None]

_PROGRESS_RE = re.compile(r"^(?:remote: )?(?P<phase>[A-Za-z ]+?):\s+(?P<pct>\d+)%")


class GitError(Exception):
    """A git invocation that the caller cannot recover from."""


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def parse_progress(line: str) -> tuple[str, int] | None:
    match = _PROGRESS_RE.match(line.strip())
    if not match:
        return None
    return match.group("phase").strip(), int(match.group("pct"))


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # never block on a prompt
    env["LC_ALL"] = "C"  # stable English messages for stderr matching
    return env


async def _drain_stderr(stream: asyncio.StreamReader, on_progress: ProgressCb | None) -> str:
    chunks: list[str] = []
    buf = ""
    while True:
        data = await stream.read(4096)
        if not data:
            break
        text = data.decode("utf-8", errors="replace")
        chunks.append(text)
        if on_progress:
            buf += text
            parts = re.split(r"[\r\n]", buf)  # git redraws progress with \r
            buf = parts.pop()
            for part in parts:
                if part.strip():
                    on_progress(part)
    if on_progress and buf.strip():
        on_progress(buf)
    return "".join(chunks)


async def run_git(
    repo: Path | str, *args: str, on_progress: ProgressCb | None = None
) -> GitResult:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=git_env(),
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        stdout_b, stderr, _ = await asyncio.gather(
            proc.stdout.read(), _drain_stderr(proc.stderr, on_progress), proc.wait()
        )
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    code = proc.returncode if proc.returncode is not None else -1
    return GitResult(code=code, stdout=stdout_b.decode("utf-8", errors="replace"), stderr=stderr)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_runner.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/githerd/runner.py tests/test_runner.py
git commit -m "feat: async git runner with streamed progress parsing"
```

---

### Task 4: Repo discovery and snapshots

**Files:**
- Create: `src/githerd/repos.py`
- Test: `tests/test_repos.py`

**Interfaces:**
- Consumes: `runner.run_git`, `runner.GitError`, `outcomes.FileChange`.
- Produces: `discover_repos(root: Path, *, max_depth: int = 4, ignore: frozenset[str] = DEFAULT_IGNORE) -> list[Path]`; `parse_status_v2(raw: str) -> dict` (keys `branch, head, upstream, ahead, behind, dirty`); `RepoSnapshot` (fields `path, name, branch, head, upstream, ahead, behind, dirty, last_commit, error`; property `is_dirty`); `async snapshot(repo: Path) -> RepoSnapshot` (raises `GitError`); `async snapshot_all(repos: list[Path], concurrency: int = 8) -> list[RepoSnapshot]` (a failing repo becomes a snapshot with `error` set).

- [ ] **Step 1: Write the failing test** (`tests/test_repos.py`)

```python
from pathlib import Path

from githerd.outcomes import FileChange
from githerd.repos import discover_repos, parse_status_v2, snapshot, snapshot_all


def _fake_repo(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def test_discover_finds_nested_repos_sorted(tmp_path):
    _fake_repo(tmp_path / "b")
    _fake_repo(tmp_path / "group" / "a")
    assert discover_repos(tmp_path) == [tmp_path / "b", tmp_path / "group" / "a"]


def test_discover_does_not_descend_into_repos(tmp_path):
    _fake_repo(tmp_path / "outer")
    _fake_repo(tmp_path / "outer" / "vendor" / "inner")
    assert discover_repos(tmp_path) == [tmp_path / "outer"]


def test_discover_skips_ignored_and_hidden_dirs(tmp_path):
    _fake_repo(tmp_path / "node_modules" / "pkg")
    _fake_repo(tmp_path / ".hidden" / "x")
    _fake_repo(tmp_path / "real")
    assert discover_repos(tmp_path) == [tmp_path / "real"]


def test_discover_respects_max_depth(tmp_path):
    _fake_repo(tmp_path / "a" / "b" / "c" / "d" / "e")  # depth 5
    assert discover_repos(tmp_path, max_depth=4) == []
    assert discover_repos(tmp_path, max_depth=5) == [tmp_path / "a" / "b" / "c" / "d" / "e"]


def test_discover_root_itself_a_repo(tmp_path):
    _fake_repo(tmp_path)
    assert discover_repos(tmp_path) == [tmp_path]


def test_parse_status_v2_handles_all_entry_types():
    raw = (
        "# branch.oid abc123\0# branch.head main\0# branch.upstream origin/main\0"
        "# branch.ab +1 -2\0"
        "1 .M N... 100644 100644 100644 h1 h2 file with space.txt\0"
        "2 R. N... 100644 100644 100644 h1 h2 R100 new.txt\0old.txt\0"
        "u UU N... 100644 100644 100644 100644 h1 h2 h3 conflict.txt\0"
        "? untracked.txt\0"
    )
    info = parse_status_v2(raw)
    assert info["branch"] == "main"
    assert info["head"] == "abc123"
    assert info["upstream"] == "origin/main"
    assert (info["ahead"], info["behind"]) == (1, 2)
    assert info["dirty"] == [
        FileChange(status=" M", path="file with space.txt"),
        FileChange(status="R ", path="new.txt"),
        FileChange(status="UU", path="conflict.txt"),
        FileChange(status="??", path="untracked.txt"),
    ]


def test_parse_status_v2_detached_and_initial():
    info = parse_status_v2("# branch.oid (initial)\0# branch.head (detached)\0")
    assert info["branch"] is None
    assert info["head"] == ""
    assert info["upstream"] is None


async def test_snapshot_clean_repo(make_repo):
    repo = make_repo("alpha")
    snap = await snapshot(repo)
    assert snap.name == "alpha"
    assert snap.branch == "main"
    assert snap.upstream == "origin/main"
    assert (snap.ahead, snap.behind) == (0, 0)
    assert snap.dirty == []
    assert snap.last_commit == "initial"
    assert not snap.is_dirty
    assert len(snap.head) == 40


async def test_snapshot_dirty_repo(make_repo, git):
    repo = make_repo("alpha")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    (repo / "staged.txt").write_text("s\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    snap = await snapshot(repo)
    assert sorted((c.status, c.path) for c in snap.dirty) == [
        (" M", "README.md"), ("??", "new.txt"), ("A ", "staged.txt"),
    ]
    assert snap.is_dirty


async def test_snapshot_ahead_and_behind(make_repo, git, commit_local, push_upstream):
    repo = make_repo("alpha")
    commit_local(repo, "local.txt")
    push_upstream(repo, "up.txt")
    git(repo, "fetch")
    snap = await snapshot(repo)
    assert (snap.ahead, snap.behind) == (1, 1)


async def test_snapshot_all_isolates_failures(make_repo, tmp_path):
    good = make_repo("good")
    bad = tmp_path / "not-a-repo"
    bad.mkdir()
    snaps = await snapshot_all([good, bad])
    assert snaps[0].error == ""
    assert snaps[1].error != ""
    assert snaps[1].name == "not-a-repo"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_repos.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'githerd.repos'`.

- [ ] **Step 3: Implement** (`src/githerd/repos.py`)

```python
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import BaseModel, Field

from githerd.outcomes import FileChange
from githerd.runner import GitError, run_git

DEFAULT_IGNORE = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build"}
)


def discover_repos(
    root: Path, *, max_depth: int = 4, ignore: frozenset[str] = DEFAULT_IGNORE
) -> list[Path]:
    root = root.resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        if ".git" in dirnames or ".git" in filenames:  # dir, or file for worktrees
            found.append(current)
            dirnames[:] = []
            continue
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d for d in dirnames if d not in ignore and not d.startswith(".")
        )
    return sorted(found)


def parse_status_v2(raw: str) -> dict:
    """Parse `git status --porcelain=v2 --branch -z` output."""
    branch: str | None = None
    head = ""
    upstream: str | None = None
    ahead = behind = 0
    dirty: list[FileChange] = []
    fields = raw.split("\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        if field.startswith("# branch.oid "):
            oid = field.split(" ", 2)[2]
            head = "" if oid == "(initial)" else oid
        elif field.startswith("# branch.head "):
            name = field.split(" ", 2)[2]
            branch = None if name == "(detached)" else name
        elif field.startswith("# branch.upstream "):
            upstream = field.split(" ", 2)[2]
        elif field.startswith("# branch.ab "):
            _, _, plus, minus = field.split(" ")
            ahead, behind = int(plus[1:]), int(minus[1:])
        elif field.startswith("1 "):
            parts = field.split(" ", 8)
            dirty.append(FileChange(status=parts[1].replace(".", " "), path=parts[8]))
        elif field.startswith("2 "):
            parts = field.split(" ", 9)
            dirty.append(FileChange(status=parts[1].replace(".", " "), path=parts[9]))
            i += 1  # skip the separate original-path field
        elif field.startswith("u "):
            parts = field.split(" ", 10)
            dirty.append(FileChange(status=parts[1], path=parts[10]))
        elif field.startswith("? "):
            dirty.append(FileChange(status="??", path=field[2:]))
    return {
        "branch": branch, "head": head, "upstream": upstream,
        "ahead": ahead, "behind": behind, "dirty": dirty,
    }


class RepoSnapshot(BaseModel):
    path: Path
    name: str
    branch: str | None = None
    head: str = ""
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty: list[FileChange] = Field(default_factory=list)
    last_commit: str = ""
    error: str = ""

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty)


async def snapshot(repo: Path) -> RepoSnapshot:
    res = await run_git(
        repo, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal"
    )
    if not res.ok:
        raise GitError(res.stderr.strip() or "git status failed")
    info = parse_status_v2(res.stdout)
    last = await run_git(repo, "log", "-1", "--format=%s")
    return RepoSnapshot(
        path=repo, name=repo.name,
        last_commit=last.stdout.strip() if last.ok else "", **info,
    )


async def snapshot_all(repos: list[Path], concurrency: int = 8) -> list[RepoSnapshot]:
    sem = asyncio.Semaphore(concurrency)

    async def one(repo: Path) -> RepoSnapshot:
        async with sem:
            try:
                return await snapshot(repo)
            except GitError as exc:
                return RepoSnapshot(path=repo, name=repo.name, error=str(exc))

    return list(await asyncio.gather(*(one(r) for r in repos)))
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_repos.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/githerd/repos.py tests/test_repos.py
git commit -m "feat: repo discovery and status snapshots"
```

---

### Task 5: Safety classification

**Files:**
- Create: `src/githerd/safety.py`
- Test: `tests/test_safety.py`

**Interfaces:**
- Produces: `class Tier(str, Enum)` with members `READ`, `MUTATE`, `DESTRUCTIVE`; `classify(args: Sequence[str]) -> Tier`. `args` is a git argument list without the leading `git` (global options like `-C path` allowed). Unknown subcommands, empty input and unparseable input return `DESTRUCTIVE`.

- [ ] **Step 1: Write the failing test** (`tests/test_safety.py`)

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_safety.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'githerd.safety'`.

- [ ] **Step 3: Implement** (`src/githerd/safety.py`)

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_safety.py -v`
Expected: all parametrized cases pass. If a case fails, fix the handler (the table is the contract); do not weaken the table.

- [ ] **Step 5: Commit**

```bash
git add src/githerd/safety.py tests/test_safety.py
git commit -m "feat: safety tier classification for git commands"
```

---

### Task 6: Git operations (pull, fetch)

**Files:**
- Create: `src/githerd/gitops.py`
- Test: `tests/test_gitops.py`

**Interfaces:**
- Consumes: `runner.run_git`, `ProgressCb`; `repos.snapshot`; `outcomes.*`.
- Produces: `classify_failure(stderr: str, remote: str = "") -> Outcome`; `async pull(repo: Path, on_progress: ProgressCb | None = None) -> Outcome`; `async fetch(repo: Path, on_progress: ProgressCb | None = None) -> Outcome`. `pull` uses `git pull --ff-only --progress`. Outcomes: `UpToDate`, `Ok` (before/after HEAD), `BlockedDirty` (git refused because local changes would be overwritten), `Diverged`, `AuthRequired`, `NetworkError`, `Failed` (detached HEAD, no upstream, anything else). `fetch` returns `Ok(commits=<now behind>, files=0, before_head=HEAD, after_head=HEAD)` when the upstream ref moved, else `UpToDate`; it never moves HEAD, so it is never journaled.

- [ ] **Step 1: Write the failing test** (`tests/test_gitops.py`)

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_gitops.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'githerd.gitops'`.

- [ ] **Step 3: Implement** (`src/githerd/gitops.py`)

```python
from __future__ import annotations

from pathlib import Path

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Diverged, Failed, NetworkError, Ok, Outcome, UpToDate,
)
from githerd.repos import snapshot
from githerd.runner import ProgressCb, run_git

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


async def _remote_url(repo: Path, upstream: str | None) -> str:
    name = upstream.split("/", 1)[0] if upstream else "origin"
    res = await run_git(repo, "config", "--get", f"remote.{name}.url")
    return res.stdout.strip() if res.ok else ""


async def _rev(repo: Path, ref: str) -> str:
    res = await run_git(repo, "rev-parse", "--verify", "--quiet", ref)
    return res.stdout.strip() if res.ok else ""


async def pull(repo: Path, on_progress: ProgressCb | None = None) -> Outcome:
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_gitops.py -v`
Expected: 14 passed. If `test_pull_blocked_when_local_changes_would_be_overwritten` fails, print `res.stderr` to see git's actual wording and extend the marker check; the behavior (BlockedDirty) is the contract.

- [ ] **Step 5: Commit**

```bash
git add src/githerd/gitops.py tests/test_gitops.py
git commit -m "feat: pull and fetch operations returning typed outcomes"
```

---

### Task 7: Journal and undo

**Files:**
- Create: `src/githerd/journal.py`, `src/githerd/undo.py`
- Test: `tests/test_journal_undo.py`

**Interfaces:**
- Consumes: `runner.run_git`.
- Produces: `JournalEntry(repo: str, op: str, before_head: str, after_head: str)`; `OpSet(id, timestamp, description, entries: list[JournalEntry])`; `Journal(root: Path)` with `record(description, entries) -> OpSet | None` (None when `entries` is empty), `mark_undone(op_set_id)`, `last_undoable() -> OpSet | None`; the journal lives at `<root>/.githerd/journal.jsonl` and creates `<root>/.githerd/.gitignore` containing `*`. `UndoItem(repo: str, status: Literal["restored","skipped","failed"], detail: str)`; `async undo_last(root: Path, journal: Journal) -> tuple[OpSet | None, list[UndoItem]]`.
- Undo semantics: an entry is reversed with `git reset --keep <before_head>` only if the repo's current HEAD equals the entry's `after_head`; otherwise the entry is skipped. A successful reversal is journaled as a new op set (`op="undo"`, before/after swapped) so undo itself can be undone. The original op set is marked undone only if at least one repo was restored.

- [ ] **Step 1: Write the failing test** (`tests/test_journal_undo.py`)

```python
from githerd.gitops import pull
from githerd.journal import Journal, JournalEntry
from githerd.outcomes import Ok
from githerd.undo import undo_last


def _entry(repo="r", op="pull", before="a", after="b"):
    return JournalEntry(repo=repo, op=op, before_head=before, after_head=after)


def test_record_with_no_entries_returns_none(tmp_path):
    assert Journal(tmp_path).record("nothing", []) is None
    assert Journal(tmp_path).last_undoable() is None


def test_record_and_last_undoable_roundtrip(tmp_path):
    journal = Journal(tmp_path)
    first = journal.record("first", [_entry()])
    second = journal.record("second", [_entry(repo="r2")])
    assert journal.last_undoable() == second
    journal.mark_undone(second.id)
    assert journal.last_undoable() == first


def test_journal_directory_ignores_itself(tmp_path):
    Journal(tmp_path).record("x", [_entry()])
    assert (tmp_path / ".githerd" / ".gitignore").read_text(encoding="utf-8").strip() == "*"


async def _pulled(make_repo, push_upstream, git, tmp_path):
    repo = make_repo("a")
    push_upstream(repo, "new.txt")
    outcome = await pull(repo)
    assert isinstance(outcome, Ok)
    root = tmp_path / "work"
    journal = Journal(root)
    journal.record("pull 1 repo", [JournalEntry(
        repo=str(repo), op="pull",
        before_head=outcome.before_head, after_head=outcome.after_head,
    )])
    return repo, root, journal, outcome


async def test_undo_restores_previous_head(make_repo, push_upstream, git, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    op_set, items = await undo_last(root, journal)
    assert op_set is not None
    assert [i.status for i in items] == ["restored"]
    assert git(repo, "rev-parse", "HEAD") == outcome.before_head
    assert not (repo / "new.txt").exists()


async def test_undo_can_itself_be_undone(make_repo, push_upstream, git, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    await undo_last(root, journal)
    await undo_last(root, journal)  # undo the undo
    assert git(repo, "rev-parse", "HEAD") == outcome.after_head
    op_set, items = await undo_last(root, journal)
    assert op_set is None and items == []


async def test_undo_nothing_to_undo(tmp_path):
    op_set, items = await undo_last(tmp_path, Journal(tmp_path))
    assert op_set is None and items == []


async def test_undo_skips_repo_that_moved_since(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    head_after_later = git(repo, "rev-parse", "HEAD")
    op_set, items = await undo_last(root, journal)
    assert [i.status for i in items] == ["skipped"]
    assert "moved" in items[0].detail
    assert git(repo, "rev-parse", "HEAD") == head_after_later
    assert journal.last_undoable() == op_set  # not consumed


async def test_undo_unknown_op_is_skipped(tmp_path, make_repo, git):
    repo = make_repo("a")
    head = git(repo, "rev-parse", "HEAD")
    journal = Journal(tmp_path)
    journal.record("weird", [JournalEntry(repo=str(repo), op="mystery", before_head="x", after_head=head)])
    _, items = await undo_last(tmp_path, journal)
    assert [i.status for i in items] == ["skipped"]
    assert "no undo" in items[0].detail
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_journal_undo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'githerd.journal'`.

- [ ] **Step 3: Implement `src/githerd/journal.py`**

```python
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel


class JournalEntry(BaseModel):
    repo: str
    op: str
    before_head: str
    after_head: str


class OpSet(BaseModel):
    id: str
    timestamp: str
    description: str
    entries: list[JournalEntry]


class Journal:
    def __init__(self, root: Path):
        self.dir = root / ".githerd"
        self.path = self.dir / "journal.jsonl"

    def _append(self, payload: dict) -> None:
        self.dir.mkdir(exist_ok=True)
        ignore = self.dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")  # keep the journal out of any repo
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    def _lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]

    def record(self, description: str, entries: list[JournalEntry]) -> OpSet | None:
        if not entries:
            return None
        op_set = OpSet(
            id=uuid.uuid4().hex[:8],
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            description=description,
            entries=entries,
        )
        self._append({"type": "opset", **op_set.model_dump()})
        return op_set

    def mark_undone(self, op_set_id: str) -> None:
        self._append({"type": "undone", "id": op_set_id})

    def last_undoable(self) -> OpSet | None:
        lines = self._lines()
        undone = {ln["id"] for ln in lines if ln["type"] == "undone"}
        for line in reversed(lines):
            if line["type"] == "opset" and line["id"] not in undone:
                return OpSet(**{k: v for k, v in line.items() if k != "type"})
        return None
```

- [ ] **Step 4: Implement `src/githerd/undo.py`**

```python
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from githerd.journal import Journal, JournalEntry, OpSet
from githerd.runner import run_git

# Ops whose reversal is "move HEAD back to before_head, keeping local changes".
# Later plans register their own reversals (e.g. commit -> reset --soft) here.
_REVERSE_ARGS: dict[str, tuple[str, ...]] = {
    "pull": ("reset", "--keep"),
    "undo": ("reset", "--keep"),
}


class UndoItem(BaseModel):
    repo: str
    status: Literal["restored", "skipped", "failed"]
    detail: str = ""


async def undo_last(root: Path, journal: Journal) -> tuple[OpSet | None, list[UndoItem]]:
    op_set = journal.last_undoable()
    if op_set is None:
        return None, []
    items: list[UndoItem] = []
    reversals: list[JournalEntry] = []
    for entry in op_set.entries:
        repo = Path(entry.repo)
        reverse = _REVERSE_ARGS.get(entry.op)
        if reverse is None:
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail=f"no undo available for '{entry.op}'"))
            continue
        head = (await run_git(repo, "rev-parse", "HEAD")).stdout.strip()
        if head != entry.after_head:
            items.append(UndoItem(repo=entry.repo, status="skipped",
                                  detail="repo has moved since this operation; not undone"))
            continue
        res = await run_git(repo, *reverse, entry.before_head)
        if res.ok:
            items.append(UndoItem(repo=entry.repo, status="restored",
                                  detail=f"back to {entry.before_head[:7]}"))
            reversals.append(JournalEntry(
                repo=entry.repo, op="undo",
                before_head=entry.after_head, after_head=entry.before_head,
            ))
        else:
            items.append(UndoItem(repo=entry.repo, status="failed",
                                  detail=res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "git reset failed"))
    if reversals:
        journal.mark_undone(op_set.id)
        journal.record(f"undo: {op_set.description}", reversals)
    return op_set, items
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_journal_undo.py -v`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add src/githerd/journal.py src/githerd/undo.py tests/test_journal_undo.py
git commit -m "feat: operation journal and undo"
```

---

### Task 8: Bulk runner and parallel pull

**Files:**
- Create: `src/githerd/bulk.py`
- Test: `tests/test_bulk.py`

**Interfaces:**
- Consumes: `runner.parse_progress`, `ProgressCb`; `gitops.pull`; `journal.Journal`, `JournalEntry`; `outcomes.*`.
- Produces: `RepoEvent(repo: str, kind: Literal["start","progress","done"], text: str = "", percent: int | None = None, outcome: Outcome | None = None)` where `repo` is `str(path)`; `Operation = Callable[[Path, ProgressCb], Awaitable[Outcome]]`; `async run_bulk(repos, op, *, concurrency=5, on_event=None) -> dict[Path, Outcome]` (preserves input order; an exception in `op` becomes `Failed`; `CancelledError` propagates); `async pull_repos(root, repos, *, concurrency=5, on_event=None) -> dict[Path, Outcome]` which journals every `Ok` whose HEAD moved as one op set described `pull N repos`.

- [ ] **Step 1: Write the failing test** (`tests/test_bulk.py`)

```python
import asyncio

from githerd.bulk import RepoEvent, pull_repos, run_bulk
from githerd.journal import Journal
from githerd.outcomes import Failed, Ok, UpToDate


async def test_run_bulk_limits_concurrency_and_keeps_order(tmp_path):
    repos = [tmp_path / f"r{i}" for i in range(6)]
    active = 0
    peak = 0

    async def op(repo, progress):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return UpToDate()

    results = await run_bulk(repos, op, concurrency=2)
    assert peak == 2
    assert list(results) == repos


async def test_run_bulk_turns_exceptions_into_failed(tmp_path):
    good, bad = tmp_path / "good", tmp_path / "bad"

    async def op(repo, progress):
        if repo == bad:
            raise RuntimeError("kaput")
        return UpToDate()

    results = await run_bulk([good, bad], op)
    assert results[good] == UpToDate()
    assert results[bad] == Failed(message="kaput")


async def test_run_bulk_emits_start_progress_done(tmp_path):
    repo = tmp_path / "r"
    events: list[RepoEvent] = []

    async def op(repo, progress):
        progress("Receiving objects:  50% (1/2)")
        return UpToDate()

    await run_bulk([repo], op, on_event=events.append)
    assert [e.kind for e in events] == ["start", "progress", "done"]
    assert events[1].percent == 50
    assert events[1].text == "Receiving objects:  50% (1/2)"
    assert events[2].outcome == UpToDate()
    assert events[0].repo == str(repo)


async def test_pull_repos_pulls_in_parallel_and_journals(make_repo, push_upstream, git, tmp_path):
    root = tmp_path / "work"
    a, b, c = make_repo("a"), make_repo("b"), make_repo("c")
    push_upstream(a, "a.txt")
    push_upstream(b, "b.txt")
    events: list[RepoEvent] = []
    results = await pull_repos(root, [a, b, c], on_event=events.append)
    assert isinstance(results[a], Ok) and isinstance(results[b], Ok)
    assert results[c] == UpToDate()
    assert sum(1 for e in events if e.kind == "done") == 3
    op_set = Journal(root).last_undoable()
    assert op_set is not None
    assert op_set.description == "pull 3 repos"
    assert sorted(e.repo for e in op_set.entries) == sorted([str(a), str(b)])


async def test_pull_repos_without_changes_writes_no_journal(make_repo, tmp_path):
    root = tmp_path / "work"
    repo = make_repo("a")
    await pull_repos(root, [repo])
    assert Journal(root).last_undoable() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_bulk.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'githerd.bulk'`.

- [ ] **Step 3: Implement** (`src/githerd/bulk.py`)

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel

from githerd.gitops import pull
from githerd.journal import Journal, JournalEntry
from githerd.outcomes import Failed, Ok, Outcome
from githerd.runner import ProgressCb, parse_progress


class RepoEvent(BaseModel):
    repo: str
    kind: Literal["start", "progress", "done"]
    text: str = ""
    percent: int | None = None
    outcome: Outcome | None = None


EventCb = Callable[[RepoEvent], None]
Operation = Callable[[Path, ProgressCb], Awaitable[Outcome]]


async def run_bulk(
    repos: list[Path],
    op: Operation,
    *,
    concurrency: int = 5,
    on_event: EventCb | None = None,
) -> dict[Path, Outcome]:
    sem = asyncio.Semaphore(concurrency)
    emit: EventCb = on_event or (lambda event: None)

    async def one(repo: Path) -> tuple[Path, Outcome]:
        async with sem:
            emit(RepoEvent(repo=str(repo), kind="start"))

            def progress(line: str) -> None:
                parsed = parse_progress(line)
                emit(RepoEvent(
                    repo=str(repo), kind="progress", text=line.strip(),
                    percent=parsed[1] if parsed else None,
                ))

            try:
                outcome = await op(repo, progress)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one repo failing must not stop the others
                outcome = Failed(message=str(exc))
            emit(RepoEvent(repo=str(repo), kind="done", outcome=outcome))
            return repo, outcome

    return dict(await asyncio.gather(*(one(r) for r in repos)))


async def pull_repos(
    root: Path,
    repos: list[Path],
    *,
    concurrency: int = 5,
    on_event: EventCb | None = None,
) -> dict[Path, Outcome]:
    results = await run_bulk(repos, pull, concurrency=concurrency, on_event=on_event)
    entries = [
        JournalEntry(repo=str(repo), op="pull",
                     before_head=o.before_head, after_head=o.after_head)
        for repo, o in results.items()
        if isinstance(o, Ok) and o.before_head != o.after_head
    ]
    Journal(root).record(f"pull {len(repos)} repos", entries)
    return results
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_bulk.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/githerd/bulk.py tests/test_bulk.py
git commit -m "feat: bounded-concurrency bulk runner and journaled parallel pull"
```

---

### Task 9: Minimal CLI (status, pull, undo)

**Files:**
- Create: `src/githerd/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `repos.discover_repos`, `snapshot_all`; `bulk.pull_repos`, `RepoEvent`; `journal.Journal`; `undo.undo_last`; `outcomes.describe`, `summarize`.
- Produces: `app = typer.Typer(...)` with commands `status`, `pull`, `undo`; each takes `--root/-r` (default: current directory); `pull` also takes `--jobs/-j` (default 5). With no repos found, `status` and `pull` print `No git repositories found under <path>` and exit 1. Output is plain ASCII.

- [ ] **Step 1: Write the failing test** (`tests/test_cli.py`)

```python
from typer.testing import CliRunner

from githerd.cli import app

runner = CliRunner()


def test_status_lists_repos(make_repo, tmp_path):
    make_repo("alpha")
    beta = make_repo("beta")
    (beta / "README.md").write_text("dirty\n", encoding="utf-8")
    result = runner.invoke(app, ["status", "--root", str(tmp_path / "work")])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output and "clean" in result.output
    assert "beta" in result.output and "1 changed" in result.output


def test_status_with_no_repos_exits_1(tmp_path):
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "No git repositories found" in result.output


def test_pull_then_undo_end_to_end(make_repo, push_upstream, git, tmp_path):
    root = tmp_path / "work"
    a, b = make_repo("a"), make_repo("b")
    before = git(a, "rev-parse", "HEAD")
    push_upstream(a, "new.txt")

    pulled = runner.invoke(app, ["pull", "--root", str(root)])
    assert pulled.exit_code == 0, pulled.output
    assert "updated: 1 commit, 1 file" in pulled.output
    assert "already up to date" in pulled.output
    assert "1 updated, 1 up to date" in pulled.output
    assert git(a, "rev-parse", "HEAD") != before

    undone = runner.invoke(app, ["undo", "--root", str(root)])
    assert undone.exit_code == 0, undone.output
    assert "restored" in undone.output
    assert git(a, "rev-parse", "HEAD") == before

    again = runner.invoke(app, ["undo", "--root", str(root)])
    assert "restored" in again.output  # undo of the undo re-applies the pull
    nothing = runner.invoke(app, ["undo", "--root", str(root)])
    assert "Nothing to undo" in nothing.output
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'githerd.cli'`.

- [ ] **Step 3: Implement** (`src/githerd/cli.py`)

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer

from githerd.bulk import RepoEvent, pull_repos
from githerd.journal import Journal
from githerd.outcomes import describe, summarize
from githerd.repos import discover_repos, snapshot_all
from githerd.undo import undo_last

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Herd all your git repos with plain English.",
)

RootOpt = Annotated[
    Optional[Path],
    typer.Option("--root", "-r", help="Directory to scan (default: current directory)."),
]


def _base(root: Optional[Path]) -> Path:
    return (root or Path.cwd()).resolve()


def _repos(root: Optional[Path]) -> tuple[Path, list[Path]]:
    base = _base(root)
    repos = discover_repos(base)
    if not repos:
        typer.echo(f"No git repositories found under {base}")
        raise typer.Exit(code=1)
    return base, repos


@app.command()
def status(root: RootOpt = None) -> None:
    """Show branch, sync state and local changes for every repo."""
    _, repos = _repos(root)
    snaps = asyncio.run(snapshot_all(repos))
    width = max(len(s.name) for s in snaps)
    for s in snaps:
        if s.error:
            typer.echo(f"{s.name:<{width}}  error: {s.error}")
            continue
        branch = s.branch or "(detached)"
        sync = f"+{s.ahead}/-{s.behind}" if s.upstream else "no upstream"
        changes = f"{len(s.dirty)} changed" if s.dirty else "clean"
        typer.echo(f"{s.name:<{width}}  {branch:<16} {sync:<12} {changes}")


@app.command()
def pull(
    root: RootOpt = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=1, help="Parallel repos.")] = 5,
) -> None:
    """Pull every repo in parallel (fast-forward only)."""
    base, repos = _repos(root)
    width = max(len(r.name) for r in repos)

    def on_event(event: RepoEvent) -> None:
        if event.kind == "done" and event.outcome is not None:
            typer.echo(f"{Path(event.repo).name:<{width}}  {describe(event.outcome)}")

    results = asyncio.run(pull_repos(base, repos, concurrency=jobs, on_event=on_event))
    typer.echo(summarize(results.values()))


@app.command()
def undo(root: RootOpt = None) -> None:
    """Undo the last operation (e.g. a bulk pull)."""
    base = _base(root)
    op_set, items = asyncio.run(undo_last(base, Journal(base)))
    if op_set is None:
        typer.echo("Nothing to undo.")
        return
    typer.echo(f"Undoing: {op_set.description}")
    for item in items:
        typer.echo(f"{Path(item.repo).name}  {item.status}: {item.detail}")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the whole suite and smoke-test the real entry point**

Run:
```powershell
.venv\Scripts\python -m pytest -v
.venv\Scripts\githerd --help
```
Expected: all tests pass; `--help` lists `status`, `pull`, `undo`.

- [ ] **Step 6: Commit**

```bash
git add src/githerd/cli.py tests/test_cli.py
git commit -m "feat: minimal CLI with status, pull and undo"
```

---

## Self-Review

**Spec coverage (Plan 1 scope).**
- §2 repo discovery, status table, filters data (snapshots), fetch/pull across repos: Tasks 4, 6, 8, 9. The status *filters* ("which repos are behind/dirty") are not a CLI feature yet; the snapshot fields make them trivial in Plan 3 as tool functions.
- §4 safety tiers, unknown→destructive: Task 5. Wiring the tier into execution (confirmation) arrives with the tool registry (Plan 3) and UI (Plan 2).
- §5 outcome union, `before_head`, bounded concurrency, progress parsing from `--progress`: Tasks 2, 3, 8. Dashboard/cards/diff viewer: Plan 2.
- §5 auth handling: non-interactive git + `AuthRequired` classification: Tasks 3, 6. The suspend-and-handoff retry is Plan 2.
- §6 undo with journal, undo of undo, skip-when-moved: Task 7. The destructive-confirmation path for "moved" repos needs the UI: Plan 2.
- §7 typed failures, one repo failing never stops others: Tasks 6, 8, `snapshot_all` in Task 4.
- §9 testing against real repos with local bare remotes: every task.
- Deferred to later plans: LLM layer, agent, config/wizard, theme/UI, branching/commit/stash/log/blame/conflict/dry-run/session-log/follow-up memory.

**Placeholder scan.** No TBD/TODO; every code step contains full code.

**Type consistency.** `Outcome` members and fields are used identically in `gitops`, `bulk`, `cli`; `JournalEntry(repo, op, before_head, after_head)` matches between `journal`, `undo`, `bulk` and tests; `run_git(repo, *args, on_progress=)` and `ProgressCb` are consistent; `RepoEvent.repo` is `str(path)` everywhere; `UndoItem.status` literals match test assertions.

**Known limitations (deliberate).** `checkout <path>` without `--` cannot be told apart from a branch name, so it classifies as mutate; Plan 3's tool layer only emits `switch` for branch changes. `pull --rebase=false` is conservatively classified destructive. After undo-of-undo there is nothing further to undo (A is marked undone), which the CLI test pins down.
