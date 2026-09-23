"""Run synchronous work without abandoning its thread on cancellation."""
import asyncio


async def drain_thread(function, *args, **kwargs):
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            if not cancelled:
                raise
            break
    if cancelled:
        # Retrieve an exception from the finished worker before propagating
        # cancellation, so no detached task or unobserved failure remains.
        if not work.cancelled():
            work.exception()
        raise asyncio.CancelledError
    return work.result()
