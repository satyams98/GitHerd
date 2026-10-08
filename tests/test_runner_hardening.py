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
    assert [r.name for r in caplog.records] == ["githerd.runner", "githerd.runner"]
    assert "progress callback raised" in caplog.records[0].getMessage()


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


async def test_cancelling_a_long_git_command_returns_promptly(tmp_path):
    started = time.monotonic()
    task = asyncio.create_task(
        run_git(tmp_path, "-c", "alias.slow=!sleep 30", "slow")
    )
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 15
