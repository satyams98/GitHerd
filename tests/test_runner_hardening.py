import asyncio
import logging
import time

import pytest

from githerd import runner
from githerd.runner import _drain_stderr, _kill_tree, run_git


async def test_incremental_decoding_survives_a_split_multibyte_character():
    reader = asyncio.StreamReader()
    task = asyncio.create_task(_drain_stderr(reader, None))
    reader.feed_data(b"caf\xc3")  # first half of "é"
    await asyncio.sleep(0.02)
    reader.feed_data(b"\xa9\n")
    reader.feed_eof()
    assert await task == "café\n"


async def test_carriage_return_redraws_are_split_into_lines():
    reader = asyncio.StreamReader()
    reader.feed_data(b"Receiving objects:  10% (1/10)\rReceiving objects:  20% (2/10)\rdone\n")
    reader.feed_eof()
    lines: list[str] = []
    await _drain_stderr(reader, lines.append)
    assert lines == ["Receiving objects:  10% (1/10)", "Receiving objects:  20% (2/10)", "done"]


async def test_decoder_is_flushed_at_eof():
    # A dangling lead byte at EOF must surface as a replacement char, not vanish.
    reader = asyncio.StreamReader()
    reader.feed_data(b"abc\xc3")
    reader.feed_eof()
    lines: list[str] = []
    text = await _drain_stderr(reader, lines.append)
    assert text == "abc�"
    assert lines == ["abc�"]


async def test_a_raising_progress_callback_is_logged_not_raised(caplog):
    reader = asyncio.StreamReader()
    reader.feed_data(b"one\ntwo\n")
    reader.feed_eof()
    seen: list[str] = []

    def explode(line: str) -> None:
        seen.append(line)
        raise RuntimeError("ui bug")

    with caplog.at_level(logging.ERROR, logger="githerd.runner"):
        text = await _drain_stderr(reader, explode)
    assert text == "one\ntwo\n"
    assert seen == ["one", "two"]  # the callback keeps being called after a failure
    # Only the first failure is logged at ERROR (with traceback); repeats are DEBUG-only.
    assert [r.name for r in caplog.records] == ["githerd.runner"]
    assert "progress callback raised" in caplog.records[0].getMessage()
    assert caplog.records[0].exc_info


async def test_a_raising_progress_callback_does_not_break_git(make_repo, tmp_path):
    repo = make_repo("a")
    remote = (await run_git(repo, "remote", "get-url", "origin")).stdout.strip()

    def explode(line: str) -> None:
        raise RuntimeError("ui bug")

    result = await run_git(
        tmp_path, "clone", "--progress", remote, str(tmp_path / "dest"), on_progress=explode
    )
    assert result.ok


class _FakeProc:
    pid = 4242

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = 1


async def test_kill_tree_uses_taskkill_on_windows_then_kills(monkeypatch):
    calls: list[tuple] = []

    class FakeKiller:
        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeKiller()

    class FakeProc:
        pid = 4242
        returncode = None
        killed = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = 1

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(runner.sys, "platform", "win32")
    proc = FakeProc()
    await _kill_tree(proc)
    assert calls and calls[0][:5] == ("taskkill", "/F", "/T", "/PID", "4242")
    assert proc.killed


async def test_kill_tree_degrades_to_kill_when_taskkill_is_missing(monkeypatch):
    async def missing(*args, **kwargs):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", missing)
    monkeypatch.setattr(runner.sys, "platform", "win32")
    proc = _FakeProc()
    await _kill_tree(proc)  # must not raise
    assert proc.killed


async def test_kill_tree_skips_a_process_that_already_exited(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("must not spawn taskkill for a finished process")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fail)
    monkeypatch.setattr(runner.sys, "platform", "win32")
    proc = _FakeProc(returncode=0)
    await _kill_tree(proc)
    assert not proc.killed


async def test_kill_tree_tolerates_a_vanished_process(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "linux")

    class Gone(_FakeProc):
        def kill(self):
            raise ProcessLookupError

    await _kill_tree(Gone())  # must not raise


async def _tasklist(*flt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tasklist", *flt, "/NH",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", errors="replace")


async def _pid_alive(pid: int) -> bool:
    return f" {pid} " in await _tasklist("/FI", f"PID eq {pid}")


async def _sleep_count() -> int:
    return (await _tasklist("/FI", "IMAGENAME eq sleep.exe")).lower().count("sleep.exe")


async def test_cancelling_a_long_git_command_returns_promptly_and_leaves_no_process(tmp_path):
    # `$$` is an MSYS pid, not a Windows one; /proc/$$/winpid maps it to the real pid.
    marker = tmp_path / "pid.txt"
    script = f"!cat /proc/$$/winpid > {marker.as_posix()} && sleep 30"
    sleeps_before = await _sleep_count()
    started = time.monotonic()
    task = asyncio.create_task(run_git(tmp_path, "-c", f"alias.slow={script}", "slow"))
    deadline = time.monotonic() + 10
    while not (marker.exists() and marker.read_text().strip()):
        assert time.monotonic() < deadline, "the alias never started"
        await asyncio.sleep(0.05)
    pid = int(marker.read_text().strip())
    assert await _pid_alive(pid), "marker pid is not alive; the leak check would be vacuous"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 15
    deadline = time.monotonic() + 5
    while await _pid_alive(pid) or await _sleep_count() > sleeps_before:
        assert time.monotonic() < deadline, f"process {pid} or its sleep child survived the cancel"
        await asyncio.sleep(0.1)


async def test_guarded_logs_a_traceback_only_for_the_first_failure(caplog):
    reader = asyncio.StreamReader()
    reader.feed_data(b"".join(b"line%d\n" % i for i in range(5)))
    reader.feed_eof()

    def explode(line: str) -> None:
        raise RuntimeError("ui bug")

    with caplog.at_level(logging.DEBUG, logger="githerd.runner"):
        await _drain_stderr(reader, explode)
    with_tb = [r for r in caplog.records if r.exc_info]
    assert len(with_tb) == 1
    assert with_tb[0].levelno == logging.ERROR
    later = [r for r in caplog.records if r is not with_tb[0]]
    assert len(later) == 4
    assert all(r.levelno == logging.DEBUG and not r.exc_info for r in later)


async def test_guarded_flag_is_per_closure(caplog):
    def explode(line: str) -> None:
        raise RuntimeError("ui bug")

    first, second = runner._guarded(explode), runner._guarded(explode)
    with caplog.at_level(logging.DEBUG, logger="githerd.runner"):
        first("a")
        second("b")
    assert sum(1 for r in caplog.records if r.exc_info) == 2


async def test_kill_tree_still_kills_when_the_taskkill_wait_is_cancelled(monkeypatch):
    class FakeKiller:
        async def wait(self):
            raise asyncio.CancelledError

    async def fake_exec(*args, **kwargs):
        return FakeKiller()

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(runner.sys, "platform", "win32")
    proc = _FakeProc()
    with pytest.raises(asyncio.CancelledError):
        await _kill_tree(proc)
    assert proc.killed


# ---- H3 item 1: bounded stdout (max_stdout_bytes) -------------------------------------

async def _git_count() -> int:
    return (await _tasklist("/FI", "IMAGENAME eq git.exe")).lower().count("git.exe")


def _big_text(path, size=5_000_000):
    line = "0123456789abcdef" * 4 + "\n"  # 65 bytes
    path.write_text(line * (size // len(line)), encoding="utf-8", newline="\n")


async def test_max_stdout_bytes_truncates_kills_and_is_quick(make_repo):
    repo = make_repo("a")
    _big_text(repo / "big.txt")
    git_before = await _git_count()
    started = time.monotonic()
    result = await run_git(
        repo, "diff", "--no-index", "--", "/dev/null", "big.txt", max_stdout_bytes=64_000
    )
    elapsed = time.monotonic() - started
    assert result.truncated is True
    assert 0 < len(result.stdout.encode()) <= 64_000 + 4096
    assert elapsed < 10
    deadline = time.monotonic() + 5
    while await _git_count() > git_before:
        assert time.monotonic() < deadline, "a git process survived the truncation"
        await asyncio.sleep(0.1)


async def test_without_the_cap_nothing_is_truncated(make_repo):
    repo = make_repo("a")
    _big_text(repo / "big.txt", 300_000)
    result = await run_git(repo, "diff", "--no-index", "--", "/dev/null", "big.txt")
    assert result.truncated is False
    assert len(result.stdout) > 300_000
    assert result.code == 1  # --no-index: "the files differ"


async def _blob(repo, content: bytes) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=content, capture_output=True, check=True,
    )
    return done.stdout.decode().strip()


async def test_output_exactly_at_the_cap_is_not_truncated(make_repo):
    repo = make_repo("a")
    sha = await _blob(repo, b"a" * 1000)
    exact = await run_git(repo, "cat-file", "-p", sha, max_stdout_bytes=1000)
    assert exact.truncated is False and exact.stdout == "a" * 1000 and exact.ok
    over = await run_git(repo, "cat-file", "-p", sha, max_stdout_bytes=999)
    assert over.truncated is True and len(over.stdout) <= 999


async def test_truncation_never_leaves_half_a_multibyte_character(make_repo):
    repo = make_repo("a")
    sha = await _blob(repo, "é".encode() * 1000)  # 2 bytes per character
    result = await run_git(repo, "cat-file", "-p", sha, max_stdout_bytes=1001)
    assert result.truncated is True
    assert "�" not in result.stdout and set(result.stdout) == {"é"}


async def test_the_cap_does_not_disturb_stderr_or_small_output(make_repo):
    repo = make_repo("a")
    ok = await run_git(repo, "rev-parse", "--abbrev-ref", "HEAD", max_stdout_bytes=1000)
    assert ok.ok and ok.stdout.strip() == "main" and ok.truncated is False
    bad = await run_git(repo, "cat-file", "-p", "deadbeef", max_stdout_bytes=1000)
    assert not bad.ok and bad.stderr and bad.truncated is False


async def test_a_capped_read_holds_at_most_the_cap_plus_one_chunk():
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * 100_000)
    reader.feed_eof()
    data, truncated = await runner._read_capped(reader, 1000)
    assert truncated is True and data == b"x" * 1000


def test_git_result_truncated_defaults_to_false():
    from githerd.runner import GitResult

    assert GitResult(code=0, stdout="", stderr="").truncated is False
