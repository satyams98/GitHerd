import asyncio
import threading

import pytest

from githerd.asyncutil import run_coro_sync


async def _value(x):
    await asyncio.sleep(0)
    return x


async def _boom():
    await asyncio.sleep(0)
    raise ValueError("kaboom")


def test_returns_the_coroutine_result():
    assert run_coro_sync(_value(42)) == 42


def test_propagates_the_coroutine_exception():
    with pytest.raises(ValueError, match="kaboom"):
        run_coro_sync(_boom())


async def test_works_while_an_event_loop_is_running_in_the_calling_thread():
    with pytest.raises(RuntimeError):  # the premise: asyncio.run is refused here
        coro = _value(1)
        try:
            asyncio.run(coro)
        finally:
            coro.close()
    assert run_coro_sync(_value("inside a loop")) == "inside a loop"


def test_runs_on_a_worker_thread_with_its_own_loop():
    async def where():
        return threading.current_thread(), asyncio.get_running_loop()

    thread, loop = run_coro_sync(where())
    assert thread is not threading.current_thread()
    assert loop.is_closed()  # asyncio.run closed its loop


def test_is_repeatable_and_leaves_no_thread_behind():
    before = threading.active_count()
    assert [run_coro_sync(_value(i)) for i in range(5)] == list(range(5))
    assert threading.active_count() == before
