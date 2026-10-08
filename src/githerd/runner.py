from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ProgressCb = Callable[[str], None]

log = logging.getLogger("githerd.runner")

_PROGRESS_RE = re.compile(r"^(?:remote: )?(?P<phase>[A-Za-z ]+?):\s+(?P<pct>\d+)%")


class GitError(Exception):
    """A git invocation that the caller cannot recover from."""


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: str
    stderr: str
    # True when run_git(max_stdout_bytes=...) stopped reading and killed git: stdout is a
    # PARTIAL prefix of the output and ``code`` is whatever the killed process reported, so
    # callers must treat the result as incomplete rather than as a success or failure.
    truncated: bool = False

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
    env["GCM_INTERACTIVE"] = "never"  # Git Credential Manager must not open a prompt
    return env


def _guarded(on_progress: ProgressCb | None) -> ProgressCb | None:
    if on_progress is None:
        return None

    failed_before = False

    def safe(line: str) -> None:
        nonlocal failed_before
        try:
            on_progress(line)
        except Exception as exc:  # a UI bug must never abort a git operation
            if failed_before:  # a broken callback fires per line; don't flood the log
                log.debug("progress callback raised again: %r", exc)
            else:
                failed_before = True
                log.exception("progress callback raised")

    return safe


async def _drain_stderr(stream: asyncio.StreamReader, on_progress: ProgressCb | None) -> str:
    on_progress = _guarded(on_progress)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    chunks: list[str] = []
    buf = ""
    while True:
        data = await stream.read(4096)
        if not data:
            break
        text = decoder.decode(data)
        chunks.append(text)
        if on_progress:
            buf += text
            parts = re.split(r"[\r\n]", buf)  # git redraws progress with \r
            buf = parts.pop()
            for part in parts:
                if part.strip():
                    on_progress(part)
    tail = decoder.decode(b"", final=True)
    if tail:
        chunks.append(tail)
        buf += tail
    if on_progress and buf.strip():
        on_progress(buf)
    return "".join(chunks)


async def _kill_tree(proc) -> None:
    """Kill git and the helpers it spawned (fetch/merge children keep pipes open)."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            except Exception:
                log.exception("taskkill failed for pid %s", proc.pid)
    finally:
        # A second cancellation during taskkill must not skip the direct kill.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)


_STDOUT_CHUNK = 16384


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    """Read at most ``cap`` bytes; ``(data, True)`` as soon as more than ``cap`` have arrived.

    Memory stays bounded: at most ``cap`` plus one chunk is ever held, and the excess is dropped.
    """
    buf = bytearray()
    while True:
        data = await stream.read(_STDOUT_CHUNK)
        if not data:
            return bytes(buf), False
        buf += data
        if len(buf) > cap:
            del buf[cap:]
            return bytes(buf), True


async def _discard(stream: asyncio.StreamReader) -> None:
    while await stream.read(_STDOUT_CHUNK):
        pass


async def run_git(
    repo: Path | str,
    *args: str,
    on_progress: ProgressCb | None = None,
    max_stdout_bytes: int | None = None,
) -> GitResult:
    """Run git and collect its output.

    With ``max_stdout_bytes`` set, reading stops once more than that many stdout bytes have
    arrived: the whole process tree is killed and the result has ``truncated=True``. Its
    stdout is then a partial prefix and its exit code is whatever the killed process reports,
    so callers must treat a truncated result as incomplete.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo), *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=git_env(),
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found; install Git for Windows") from exc
    assert proc.stdout is not None and proc.stderr is not None
    truncated = False

    async def read_stdout() -> bytes:
        nonlocal truncated
        if max_stdout_bytes is None:
            return await proc.stdout.read()
        data, truncated = await _read_capped(proc.stdout, max_stdout_bytes)
        if truncated:  # stop git; the stderr drain and proc.wait() finish once it is gone
            await _kill_tree(proc)
            # proc.wait() only completes once the stdout pipe is closed too, so read (and
            # discard) what is left until EOF; the dead process cannot add more.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(_discard(proc.stdout), 5)
        return data

    try:
        stdout_b, stderr, _ = await asyncio.gather(
            read_stdout(), _drain_stderr(proc.stderr, on_progress), proc.wait()
        )
    except BaseException:
        await _kill_tree(proc)
        raise
    code = proc.returncode if proc.returncode is not None else -1
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    # A cut in the middle of a character drops the fragment instead of showing U+FFFD.
    stdout = decoder.decode(stdout_b, final=not truncated)
    return GitResult(code=code, stdout=stdout, stderr=stderr, truncated=truncated)
