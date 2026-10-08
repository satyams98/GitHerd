from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Coroutine, TypeVar

T = TypeVar("T")


def run_coro_sync(coro: Coroutine[object, object, T]) -> T:
    """Run ``coro`` to completion from synchronous code and return its result.

    ``asyncio.run`` raises RuntimeError when the calling thread already runs an event loop
    (e.g. inside a prompt_toolkit key handler), so the coroutine runs in a fresh loop on a
    short-lived worker thread; the caller blocks until it finishes. Exceptions propagate.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
