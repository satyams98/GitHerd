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


async def _kill_tree(proc, *, wait: bool = True) -> None:
    """Kill git and the helpers it spawned (fetch/merge children keep pipes open).

    ``wait=False`` skips the final bounded wait for the process, for callers that bound
    the whole post-kill phase themselves.
    """
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
        if wait:
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


# Overall budget (seconds) for the phase after a capped read killed git: process exit, the end
# of stdout and the end of the stderr drain. A descendant that inherited the pipes and
# outlived git (a backgrounded helper) would otherwise hold them open for ever.
KILL_SETTLE_SECONDS = 5.0


def _abandon_pipes(proc) -> None:
    """Close the transport so pipes a stray descendant still holds cannot keep us waiting."""
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


async def _settle_after_kill(proc, stderr_task: asyncio.Future, wait_task: asyncio.Future) -> str:
    """Wait (bounded) for a killed git to exit and its pipes to end; returns what stderr gave.

    ``proc.wait()`` only completes once the stdout pipe is closed too, so stdout is read (and
    discarded) until EOF meanwhile. Whatever is not finished within ``KILL_SETTLE_SECONDS`` is
    abandoned: its task is cancelled and the pipes are closed, and the caller returns anyway.
    """
    discard_task = asyncio.ensure_future(_discard(proc.stdout))
    try:
        done, pending = await asyncio.wait(
            {discard_task, stderr_task, wait_task}, timeout=KILL_SETTLE_SECONDS
        )
    except BaseException:
        # Cancelled (or interrupted) while waiting: the caller cancels its own tasks, but
        # ``discard_task`` is ours, so it must not be left reading a pipe a descendant holds.
        discard_task.cancel()
        _abandon_pipes(proc)
        raise
    for task in done:
        if not task.cancelled():
            task.exception()  # retrieved, so a failed drain is not reported as never-retrieved
    stderr = ""
    if stderr_task in done and not stderr_task.cancelled() and stderr_task.exception() is None:
        stderr = stderr_task.result()
    if pending:
        log.warning(
            "git (pid %s) was killed but its pipes stayed open for %gs; abandoning them",
            getattr(proc, "pid", "?"), KILL_SETTLE_SECONDS,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        _abandon_pipes(proc)
    return stderr


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
    so callers must treat a truncated result as incomplete. After the kill, waiting for git
    and the pipes is bounded by ``KILL_SETTLE_SECONDS``: a descendant that keeps a pipe open
    cannot hang the call. A negative cap raises ``ValueError``.
    """
    if max_stdout_bytes is not None and max_stdout_bytes < 0:
        raise ValueError("max_stdout_bytes must be >= 0")
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
        return data

    stdout_task = asyncio.ensure_future(read_stdout())
    stderr_task = asyncio.ensure_future(_drain_stderr(proc.stderr, on_progress))
    wait_task = asyncio.ensure_future(proc.wait())
    try:
        if max_stdout_bytes is None:
            stdout_b, stderr, _ = await asyncio.gather(stdout_task, stderr_task, wait_task)
        else:
            stdout_b = await stdout_task
            if truncated:  # stop git, then give its exit and pipes a bounded time to settle
                await _kill_tree(proc, wait=False)
                stderr = await _settle_after_kill(proc, stderr_task, wait_task)
            else:
                stderr, _ = await asyncio.gather(stderr_task, wait_task)
    except BaseException:
        for task in (stdout_task, stderr_task, wait_task):
            task.cancel()
        await _kill_tree(proc)
        raise
    code = proc.returncode if proc.returncode is not None else -1
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    # A cut in the middle of a character drops the fragment instead of showing U+FFFD.
    stdout = decoder.decode(stdout_b, final=not truncated)
    return GitResult(code=code, stdout=stdout, stderr=stderr, truncated=truncated)
