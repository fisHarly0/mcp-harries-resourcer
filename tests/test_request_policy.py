import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

from request_policy import PageCache, RequestPacer, RequestPolicy
import server


REAL_CLIENT = httpx.AsyncClient


def page(text="Body", url="https://example.org"):
    return {"url": url, "ok": True, "error": "", "title": "Title", "text": text}


class Clock:
    def __init__(self):
        self.now = 100.0
        self.delays = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class CacheTests(unittest.TestCase):
    def test_ttl_does_not_extend_on_reads(self):
        clock = Clock()
        cache = PageCache(ttl=10, clock=clock)
        cache.put("url", page())
        clock.now += 9
        self.assertEqual(cache.get("url")["cache_age_seconds"], 9)
        clock.now += 1
        self.assertIsNone(cache.get("url"))
        self.assertEqual(cache.total_bytes, 0)

    def test_lru_evicts_least_recently_read(self):
        cache = PageCache(max_entries=2)
        cache.put("a", page())
        cache.put("b", page())
        cache.get("a")
        cache.put("c", page())
        self.assertIsNone(cache.get("b"))
        self.assertIsNotNone(cache.get("a"))
        self.assertIsNotNone(cache.get("c"))

    def test_byte_budget_and_oversized_page(self):
        cache = PageCache(max_bytes=20)
        cache.put("a", page("文" * 4))  # 1 byte URL + 5 byte title + 12 byte body
        cache.put("b", page("文" * 4))
        self.assertIsNone(cache.get("a"))
        self.assertLessEqual(cache.total_bytes, 20)
        cache.put("big", page("文" * 20))
        self.assertIsNone(cache.get("big"))
        self.assertIsNotNone(cache.get("b"))

    def test_returned_page_does_not_mutate_cached_page(self):
        cache = PageCache()
        result = page("Complete text")
        cache.put("url", result)
        result["text"] = "mutated by caller"
        cached = cache.get("url")
        cached["text"] = "short"
        self.assertEqual(cache.get("url")["text"], "Complete text")

    def test_failures_are_not_cached(self):
        cache = PageCache()
        cache.put("url", {**page(), "ok": False, "error": "HTTP 429"})
        self.assertIsNone(cache.get("url"))

    def test_any_zero_cache_setting_disables_storage(self):
        for setting in ("ttl", "max_entries", "max_bytes"):
            with self.subTest(setting=setting):
                cache = PageCache(**{setting: 0})
                cache.put("url", page())
                self.assertIsNone(cache.get("url"))

    def test_expired_entries_are_removed_on_insert(self):
        clock = Clock()
        cache = PageCache(ttl=10, clock=clock)
        cache.put("a", page())
        clock.now += 10
        cache.put("b", page())
        self.assertEqual(list(cache.entries), ["b"])


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_search_calls_share_spacing(self):
        clock = Clock()
        policy = RequestPolicy(search_rpm=30, clock=clock, sleep=clock.sleep)
        starts = []
        async def search():
            await policy.wait_for_search("ddg")
            starts.append(clock())
        await asyncio.gather(*(search() for _ in range(4)))
        self.assertEqual(starts, [100, 102, 104, 106])

    async def test_search_engines_have_independent_budgets(self):
        clock = Clock()
        policy = RequestPolicy(search_rpm=30, clock=clock, sleep=clock.sleep)
        await policy.wait_for_search("ddg")
        await policy.wait_for_search("bing")
        self.assertEqual(clock.delays, [])

    async def test_zero_rpm_disables_pacing(self):
        clock = Clock()
        pacer = RequestPacer(0, clock=clock, sleep=clock.sleep)
        await asyncio.gather(*(pacer.wait() for _ in range(10)))
        self.assertEqual(clock.delays, [])

    async def test_cancelled_waiter_releases_lock_without_reserving_slot(self):
        clock = Clock()
        sleeping = asyncio.Event()
        async def sleep(delay):
            sleeping.set()
            await asyncio.Event().wait()
        pacer = RequestPacer(60, clock=clock, sleep=sleep)
        await pacer.wait()
        waiter = asyncio.create_task(pacer.wait())
        await sleeping.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(pacer.next_start, 101)
        clock.now = 101
        await asyncio.wait_for(pacer.wait(), timeout=1)

    async def test_invalid_environment_falls_back(self):
        with patch.dict(os.environ, {
            "RESOURCER_SEARCH_RPM": "NaN", "RESOURCER_FETCH_RPM": "-1",
            "RESOURCER_CACHE_TTL_SECONDS": "Infinity", "RESOURCER_CACHE_MAX_ENTRIES": "999999",
            "RESOURCER_CACHE_MAX_BYTES": "-5",
        }), self.assertLogs("request_policy", level="WARNING"):
            policy = RequestPolicy.from_env()
        self.assertEqual(policy.search_pacers["ddg"].interval, 2)
        self.assertEqual(policy.fetch_pacer.interval, 1)
        self.assertEqual(policy.cache.ttl, 300)
        self.assertEqual(policy.cache.max_entries, 64)
        self.assertEqual(policy.cache.max_bytes, 8 * 1024 * 1024)


class FetchPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_inflight_requests_share_one_download(self):
        policy = RequestPolicy(fetch_rpm=0)
        calls = 0
        async def loader():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return page()
        results = await asyncio.gather(*(policy.fetch("url", loader) for _ in range(5)))
        self.assertEqual(calls, 1)
        self.assertTrue(all(r["text"] == "Body" for r in results))
        self.assertEqual(policy.inflight, {})

    async def test_fetches_share_global_pacing(self):
        clock = Clock()
        policy = RequestPolicy(fetch_rpm=60, clock=clock, sleep=clock.sleep)
        starts = []
        async def loader():
            starts.append(clock())
            return page()
        await asyncio.gather(*(policy.fetch(str(i), loader) for i in range(3)))
        self.assertEqual(starts, [100, 101, 102])

    async def test_separate_batches_share_global_concurrency(self):
        policy = RequestPolicy(fetch_rpm=0, fetch_concurrency=5)
        active = peak = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        async def loader():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 5:
                entered.set()
            try:
                await release.wait()
                return page()
            finally:
                active -= 1
        async def batch(prefix):
            return await asyncio.gather(*(policy.fetch(f"{prefix}/{i}", loader) for i in range(8)))
        batches = [asyncio.create_task(batch(prefix)) for prefix in ("a", "b")]
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        await asyncio.gather(*batches)
        self.assertEqual(peak, 5)

    async def test_cancelled_follower_does_not_cancel_owner(self):
        policy = RequestPolicy(fetch_rpm=0)
        entered, release = asyncio.Event(), asyncio.Event()
        async def loader():
            entered.set()
            await release.wait()
            return page()
        owner = asyncio.create_task(policy.fetch("url", loader))
        await entered.wait()
        follower = asyncio.create_task(policy.fetch("url", loader))
        await asyncio.sleep(0)
        follower.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await follower
        release.set()
        self.assertTrue((await owner)["ok"])
        self.assertEqual(policy.inflight, {})

    async def test_cancelled_owner_allows_follower_to_retry_with_own_loader(self):
        policy = RequestPolicy(fetch_rpm=0)
        entered = asyncio.Event()
        async def first_loader():
            entered.set()
            await asyncio.Event().wait()
        async def second_loader():
            return page("Second client remains usable")
        owner = asyncio.create_task(policy.fetch("url", first_loader))
        await entered.wait()
        follower = asyncio.create_task(policy.fetch("url", second_loader))
        await asyncio.sleep(0)
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        result = await asyncio.wait_for(follower, 1)
        self.assertEqual(result["text"], "Second client remains usable")
        self.assertEqual(policy.inflight, {})

    async def test_failed_page_is_retried_on_next_call(self):
        policy = RequestPolicy(fetch_rpm=0)
        calls = 0
        async def loader():
            nonlocal calls
            calls += 1
            return {**page(), "ok": calls > 1, "error": "HTTP 503" if calls == 1 else ""}
        self.assertFalse((await policy.fetch("url", loader))["ok"])
        self.assertTrue((await policy.fetch("url", loader))["ok"])
        self.assertEqual(calls, 2)

    async def test_loader_exception_does_not_leave_stale_flight(self):
        policy = RequestPolicy(fetch_rpm=0)
        async def loader():
            raise ValueError("failed")
        with self.assertRaises(ValueError):
            await policy.fetch("url", loader)
        self.assertEqual(policy.inflight, {})


class FetchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        policy = patch.object(server, "NETWORK", RequestPolicy(search_rpm=0, fetch_rpm=0))
        policy.start()
        self.addCleanup(policy.stop)

    async def test_short_first_read_does_not_poison_longer_cached_read(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(200, text="<html><title>Guide</title></html>")
        async with REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
            with patch.object(server.trafilatura, "extract", return_value="Complete article body"):
                short = await server._fetch_one(client, "https://example.org/a", 4)
                full = await server._fetch_one(client, "https://example.org/a", 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(short["text"], "Comp")
        self.assertTrue(short["truncated"])
        self.assertEqual(full["text"], "Complete article body")
        self.assertFalse(full["truncated"])
        self.assertTrue(full["cached"])

    async def test_public_tools_share_cache_and_show_provenance(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(200, text="<html><title>Guide</title></html>")
        def client_factory(**kwargs):
            return REAL_CLIENT(transport=httpx.MockTransport(handler))
        with patch.object(server.httpx, "AsyncClient", side_effect=client_factory):
            with patch.object(server.trafilatura, "extract", return_value="Shared cached article"):
                first = await server.fetch_page("https://example.org/a")
                second = await server.fetch_pages(["https://example.org/a"])
        self.assertEqual(len(requests), 1)
        self.assertNotIn("进程内缓存", first)
        self.assertIn("进程内缓存", second)

    async def test_search_requests_are_paced_at_send_boundary(self):
        clock = Clock()
        policy = RequestPolicy(search_rpm=30, clock=clock, sleep=clock.sleep)
        starts = []
        def handler(request):
            starts.append(clock())
            return httpx.Response(200, text='<div class="no-results">No results</div>')
        with patch.object(server, "NETWORK", policy):
            async with REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
                await asyncio.gather(server._ddg(client, "first", 1), server._ddg(client, "second", 1))
        self.assertEqual(starts, [100, 102])
