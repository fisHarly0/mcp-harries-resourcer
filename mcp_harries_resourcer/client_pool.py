"""Exclusive HTTP client leases with bounded, expiring idle reuse."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import logging


async def _finish(task):
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError()


class ClientPool:
    def __init__(self, factory, *, max_idle=5, idle_seconds=60):
        self.factory = factory
        self.max_idle, self.idle_seconds = max_idle, idle_seconds
        self.running = False
        self.idle = OrderedDict()
        self.active = set()
        self.closing = {}

    def _close(self, client):
        if client in self.closing:
            return self.closing[client]
        async def close():
            try:
                await client.aclose()
            except Exception as exc:
                logging.warning("HTTP client cleanup failed (%s)", type(exc).__name__)
        task = asyncio.create_task(close(), name="resourcer-client-close")
        self.closing[client] = task
        task.add_done_callback(lambda _: self.closing.pop(client, None))
        return task

    def _expire(self, client):
        if client in self.idle:
            self.idle.pop(client).cancel()
            self._close(client)

    @asynccontextmanager
    async def lease(self):
        pooled = self.running
        if pooled and self.idle:
            client, timer = self.idle.popitem()
            timer.cancel()
        else:
            client = self.factory()
        self.active.add(client)
        try:
            yield client
        finally:
            self.active.discard(client)
            # Cookies may span requests within one lease, never different calls.
            client.cookies.clear()
            if pooled and self.running and not client.is_closed and self.max_idle and self.idle_seconds:
                if len(self.idle) >= self.max_idle:
                    oldest, timer = self.idle.popitem(last=False)
                    timer.cancel()
                    self._close(oldest)
                self.idle[client] = asyncio.get_running_loop().call_later(self.idle_seconds, self._expire, client)
            else:
                await _finish(self._close(client))

    async def __aenter__(self):
        if self.running:
            raise RuntimeError("HTTP client pool is already running")
        self.running = True
        return self

    async def __aexit__(self, *args):
        self.running = False
        for timer in self.idle.values():
            timer.cancel()
        clients = set(self.idle) | self.active
        self.idle.clear()
        for client in clients:
            self._close(client)
        if self.closing:
            await _finish(asyncio.gather(*list(self.closing.values())))
