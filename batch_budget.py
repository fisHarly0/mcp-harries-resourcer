"""One cooperative deadline across stages, with bounded workers and partial results."""
import asyncio
from dataclasses import dataclass
import math
from task_cleanup import cancel_and_wait


@dataclass(frozen=True)
class Unfinished:
    started: bool


class BatchBudget:
    def __init__(self, seconds=120):
        if not math.isfinite(seconds) or not 0 < seconds <= 600:
            raise ValueError("time_budget_seconds 必须大于 0 且不超过 600 秒")
        self.seconds = seconds
        self.loop = asyncio.get_running_loop()
        self.started = self.loop.time()
        self.deadline = self.started + seconds
        self.exhausted = False
        self.completed = 0
        self.timed_out = 0
        self.not_started = 0

    def remaining(self):
        return 0.0 if self.exhausted else max(0.0, self.deadline - self.loop.time())

    def metadata(self):
        return {
            "time_budget_seconds": self.seconds,
            "elapsed_seconds": round(self.loop.time() - self.started, 3),
            "deadline_exceeded": not bool(self.remaining()),
            "complete": self.timed_out == 0 and self.not_started == 0,
            "completed": self.completed, "timed_out": self.timed_out,
            "not_started": self.not_started,
        }

    async def collect(self, items, operation, concurrency=5):
        """Return values/errors in input order; await cleanup before releasing client."""
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        missing = object()
        results = [missing] * len(items)
        started = [False] * len(items)
        pending = iter(range(len(items)))

        async def worker():
            for index in pending:
                if not self.remaining():
                    return
                started[index] = True
                try:
                    results[index] = await operation(items[index])
                except Exception as exc:
                    results[index] = exc

        tasks = []
        if items and self.remaining():
            tasks = [asyncio.create_task(worker(), name="resourcer-batch-worker")
                     for _ in range(min(concurrency, len(items)))]
        try:
            if tasks:
                _, pending_tasks = await asyncio.wait(tasks, timeout=self.remaining())
                if pending_tasks:
                    # Timer resolution can wake wait() just before loop.time()
                    # crosses the deadline (notably on Windows). Once it fires,
                    # the next stage must not regain that fractional budget.
                    self.exhausted = True
                # A worker cancelling itself must propagate cancellation, not look like timeout.
                if any(task.cancelled() for task in tasks):
                    raise asyncio.CancelledError()
        finally:
            if tasks:
                await cancel_and_wait(tasks)

        for index, result in enumerate(results):
            if result is missing:
                results[index] = Unfinished(started[index])
                if started[index]:
                    self.timed_out += 1
                else:
                    self.not_started += 1
            else:
                self.completed += 1
        return results
