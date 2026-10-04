"""Process-local request pacing, bounded page cache and duplicate fetch sharing."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import os
import time

from http_policy import HTTPPolicy


def _setting(name: str, default: int, maximum: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    logging.getLogger(__name__).warning("Invalid %s; using default %s", name, default)
    return default


class RequestPacer:
    """Space request starts evenly; a cancelled waiter does not reserve a slot."""

    def __init__(self, rpm: int, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.interval = 60.0 / rpm if rpm > 0 else 0
        self.clock = clock
        self.sleep = sleep
        self.next_start = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        if not self.interval:
            return
        async with self.lock:
            delay = self.next_start - self.clock()
            while delay > 0:
                await self.sleep(delay)
                delay = self.next_start - self.clock()
            self.next_start = self.clock() + self.interval


@dataclass
class CacheEntry:
    result: dict
    created_at: float
    size: int


class PageCache:
    """TTL applies from fetch completion; hits update LRU order, not expiry."""

    def __init__(self, ttl=300, max_entries=64, max_bytes=8 * 1024 * 1024, *, clock=time.monotonic):
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.clock = clock
        self.entries = OrderedDict()
        self.total_bytes = 0

    def _drop(self, key):
        self.total_bytes -= self.entries.pop(key).size

    def _expire(self):
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if now - entry.created_at >= self.ttl:
                self._drop(key)

    def get(self, url):
        self._expire()
        entry = self.entries.get(url)
        if entry is None:
            return None
        self.entries.move_to_end(url)
        return {**entry.result, "cached": True, "cache_age_seconds": int(self.clock() - entry.created_at)}

    def invalidate(self, url):
        if url in self.entries:
            self._drop(url)

    def put(self, url, result):
        if not result.get("ok") or not self.ttl or not self.max_entries or not self.max_bytes:
            return
        self._expire()
        size = sum(len(str(value).encode("utf-8")) for value in (url, result.get("title", ""), result.get("text", "")))
        if size > self.max_bytes:
            return
        if url in self.entries:
            self._drop(url)
        while self.entries and (len(self.entries) >= self.max_entries or self.total_bytes + size > self.max_bytes):
            self._drop(next(iter(self.entries)))
        self.entries[url] = CacheEntry(dict(result), self.clock(), size)
        self.total_bytes += size


class RequestPolicy:
    """One instance per server event loop; no persistent cache or extra worker tasks."""

    def __init__(self, search_rpm=30, fetch_rpm=60, cache_ttl=300, cache_entries=64,
                 cache_bytes=8 * 1024 * 1024, fetch_concurrency=5, *, clock=time.monotonic,
                 sleep=asyncio.sleep, http_policy=None):
        self.http = http_policy if http_policy is not None else HTTPPolicy(clock=clock, sleep=sleep)
        self.search_pacers = {engine: RequestPacer(search_rpm, clock=clock, sleep=sleep) for engine in ("ddg", "bing")}
        self.fetch_pacer = RequestPacer(fetch_rpm, clock=clock, sleep=sleep)
        self.fetch_slots = asyncio.Semaphore(fetch_concurrency)
        self.cache = PageCache(cache_ttl, cache_entries, cache_bytes, clock=clock)
        self.inflight = {}

    @classmethod
    def from_env(cls):
        return cls(
            search_rpm=_setting("RESOURCER_SEARCH_RPM", 30, 60000),
            fetch_rpm=_setting("RESOURCER_FETCH_RPM", 60, 60000),
            cache_ttl=_setting("RESOURCER_CACHE_TTL_SECONDS", 300, 86400),
            cache_entries=_setting("RESOURCER_CACHE_MAX_ENTRIES", 64, 4096),
            cache_bytes=_setting("RESOURCER_CACHE_MAX_BYTES", 8 * 1024 * 1024, 512 * 1024 * 1024),
            http_policy=HTTPPolicy(
                total_timeout=_setting("RESOURCER_REQUEST_TIMEOUT_SECONDS", 60, 300, 1),
                max_bytes=_setting("RESOURCER_RESPONSE_MAX_BYTES", 8 * 1024 * 1024, 64 * 1024 * 1024, 1024),
                retries=_setting("RESOURCER_HTTP_RETRIES", 1, 3),
            ),
        )

    async def wait_for_search(self, engine):
        await self.search_pacers[engine].wait()

    async def fetch(self, url, loader, *, refresh=False):
        if refresh:
            # Discard a completed snapshot once. An in-progress download can still
            # be shared, so concurrent refreshes don't duplicate network traffic.
            self.cache.invalidate(url)
        while True:
            cached = self.cache.get(url)
            if cached is not None:
                return cached
            existing = self.inflight.get(url)
            if existing is not None:
                result = await asyncio.shield(existing)
                if result is None:
                    # The owner was cancelled. Retry using this caller's live client.
                    continue
                return dict(result)

            flight = asyncio.get_running_loop().create_future()
            self.inflight[url] = flight
            try:
                async with self.fetch_slots:
                    result = {**await loader(), "cached": False, "cache_age_seconds": 0}
                self.cache.put(url, result)
                flight.set_result(result)
                return dict(result)
            except BaseException:
                # Resolve (rather than cancel) so a cancelled owner cannot cancel followers.
                flight.set_result(None)
                raise
            finally:
                self.inflight.pop(url, None)
