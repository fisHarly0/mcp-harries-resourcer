"""Keep ownership of cancelled child tasks until their cleanup is complete."""
import asyncio


async def cancel_and_wait(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    drained = asyncio.gather(*tasks, return_exceptions=True)
    cancelled = False
    while not drained.done():
        try:
            await asyncio.shield(drained)
        except asyncio.CancelledError:
            cancelled = True
    drained.result()
    if cancelled:
        raise asyncio.CancelledError()


async def wait_for_owned(awaitable, timeout):
    operation = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(operation, timeout)
    finally:
        # In Python 3.10 a second cancellation can interrupt wait_for's wait for
        # child cleanup. Retain ownership at every enclosing timeout boundary.
        if not operation.done():
            await cancel_and_wait([operation])
