"""Per-call, coalesced MCP progress with bounded notification delivery."""
import asyncio
from contextvars import ContextVar
from functools import wraps
from inspect import signature

from .task_cleanup import cancel_and_wait, wait_for_owned


_current = ContextVar("resourcer_progress", default=None)


class Progress:
    def __init__(self, context, *, interval=0.1, timeout=0.25):
        self.context = context
        self.interval, self.timeout = interval, timeout
        self.pending = None
        self.last = -1
        self.wake = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.task = None
        self.disabled = context is None

    def update(self, value, total, message):
        if self.disabled or value <= self.last:
            return
        self.last = value
        self.pending = (value, total, message)
        self.idle.clear()
        self.wake.set()

    async def _send(self):
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                item, self.pending = self.pending, None
                await wait_for_owned(self.context.report_progress(*item), self.timeout)
                if self.pending is None:
                    self.idle.set()
                await asyncio.sleep(self.interval)
        except Exception:
            # Optional notifications must not discard usable work or create a
            # growing backlog when the client cannot receive them.
            self.disabled = True
            self.pending = None
            self.idle.set()

    async def __aenter__(self):
        if not self.disabled:
            self.task = asyncio.create_task(self._send(), name="resourcer-progress")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.task is None:
            return
        try:
            if exc_type is None:
                try:
                    await wait_for_owned(self.idle.wait(), self.timeout + self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await cancel_and_wait([self.task])


def with_progress(function):
    parameters = signature(function)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        if _current.get() is not None:
            return await function(*args, **kwargs)
        context = parameters.bind_partial(*args, **kwargs).arguments.get("ctx")
        if context is not None:
            meta = context.request_context.meta
            if meta is None or meta.progressToken is None:
                context = None
        progress = Progress(context)
        token = _current.set(progress)
        try:
            async with progress:
                return await function(*args, **kwargs)
        finally:
            _current.reset(token)
    return wrapped


def report(value, total, message):
    current = _current.get()
    if current is not None:
        current.update(value, total, message)


class CompletionCounter:
    def __init__(self, total, unit, *, offset=0, successful=lambda result: result.get("ok", False)):
        self.total, self.unit, self.offset = total, unit, offset
        self.successful = successful
        self.done = self.ok = 0

    async def run(self, operation):
        try:
            result = await operation
        except Exception:
            self._ended(False)
            raise
        else:
            self._ended(self.successful(result))
            return result

    def _ended(self, ok):
        self.done += 1
        self.ok += int(ok)
        report(self.offset + self.done, self.offset + self.total,
               f"已结束 {self.done}/{self.total} 项{self.unit}：成功 {self.ok}，失败 {self.done - self.ok}")
