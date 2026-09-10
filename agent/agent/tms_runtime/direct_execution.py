"""Keep a live request admitted until its blocking operation has really stopped."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def call_blocking(function: Callable[..., Any], *args: Any, timeout_sec: float) -> Any:
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.wait_for(asyncio.shield(operation), timeout=timeout_sec)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Cancelling the awaiter cannot stop a running requests/DB call.
        await asyncio.gather(operation, return_exceptions=True)
        raise
