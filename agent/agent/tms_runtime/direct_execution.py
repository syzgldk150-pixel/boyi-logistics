"""Keep a live request admitted until its blocking operation has really stopped."""
from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# User-facing direct reads get their own workers. The loop's default executor
# has min(32, cpu+4) threads (6 on the 2-core ECS) and is shared with plugin
# Broker/Host calls; a running collector filled it and queued page queries
# for tens of seconds before any platform request started.
DIRECT_CALL_WORKERS = 16
_DIRECT_EXECUTOR = ThreadPoolExecutor(max_workers=DIRECT_CALL_WORKERS, thread_name_prefix="direct-call")


async def call_blocking(function: Callable[..., Any], *args: Any, timeout_sec: float) -> Any:
    # Same context propagation as asyncio.to_thread (execution scopes are contextvars).
    context = contextvars.copy_context()
    operation = asyncio.ensure_future(asyncio.get_running_loop().run_in_executor(
        _DIRECT_EXECUTOR, functools.partial(context.run, function, *args)))
    try:
        return await asyncio.wait_for(asyncio.shield(operation), timeout=timeout_sec)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Cancelling the awaiter cannot stop a running requests/DB call.
        await asyncio.gather(operation, return_exceptions=True)
        raise
