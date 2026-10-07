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
