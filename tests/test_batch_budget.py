import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from batch_budget import BatchBudget, Unfinished
from request_policy import RequestPolicy
import server


REAL_CLIENT = httpx.AsyncClient


def page(url):
    return {"url": url, "ok": True, "title": "Page", "text": "Completed body"}


def result(url):
    return {"url": url, "title": "Page", "snippet": "", "source": "ddg"}


class BatchBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_budgets(self):
        for seconds in [0, -1, 601, float("nan"), float("inf")]:
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                BatchBudget(seconds)

    async def test_deadline_preserves_results_and_marks_active_and_unstarted(self):
        cancelled = []
        async def operation(index):
            if index == 0:
                return "finished"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(index)
        budget = BatchBudget(0.03)
        values = await budget.collect(list(range(7)), operation, concurrency=2)
        self.assertEqual(values[0], "finished")
        self.assertEqual(values[1:3], [Unfinished(True), Unfinished(True)])
        self.assertEqual(values[3:], [Unfinished(False)] * 4)
        self.assertCountEqual(cancelled, [1, 2])
        self.assertEqual(budget.metadata()["completed"], 1)
        self.assertEqual(budget.metadata()["timed_out"], 2)
        self.assertEqual(budget.metadata()["not_started"], 4)
        self.assertFalse(any(t.get_name() == "resourcer-batch-worker" for t in asyncio.all_tasks()))

    async def test_errors_do_not_discard_other_results(self):
        async def operation(index):
            if index == 1:
                raise ValueError("bad item")
            return index
        budget = BatchBudget(1)
        values = await budget.collect([0, 1, 2], operation)
        self.assertEqual(values[0], 0)
        self.assertIsInstance(values[1], ValueError)
        self.assertEqual(values[2], 2)
        self.assertTrue(budget.metadata()["complete"])

    async def test_deadline_is_shared_between_stages(self):
        budget = BatchBudget(0.02)
        await budget.collect([0], lambda _: asyncio.Event().wait())
        operation = AsyncMock()
        values = await budget.collect([1, 2], operation)
        operation.assert_not_called()
        self.assertEqual(values, [Unfinished(False), Unfinished(False)])

    async def test_early_timer_signal_cannot_restart_next_stage(self):
        budget = BatchBudget(60)
        async def early_timeout(tasks, **kwargs):
            await asyncio.sleep(0)
            return set(), set(tasks)
        with patch("batch_budget.asyncio.wait", side_effect=early_timeout):
            first = await budget.collect([0], lambda _: asyncio.Event().wait())
        self.assertEqual(first, [Unfinished(True)])
        self.assertTrue(budget.metadata()["deadline_exceeded"])
        self.assertEqual(budget.remaining(), 0)
        operation = AsyncMock()
        second = await budget.collect([1], operation)
        self.assertEqual(second, [Unfinished(False)])
        operation.assert_not_called()

    async def test_external_cancellation_propagates_after_worker_cleanup(self):
        entered = asyncio.Event()
        cleaned = []
        async def operation(index):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(index)
        task = asyncio.create_task(BatchBudget(60).collect([0, 1], operation))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertCountEqual(cleaned, [0, 1])

    async def test_large_batch_only_creates_worker_count_tasks(self):
        entered = asyncio.Event()
        async def operation(index):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(BatchBudget(60).collect(list(range(10000)), operation, concurrency=3))
        await entered.wait()
        self.assertEqual(sum(t.get_name() == "resourcer-batch-worker" for t in asyncio.all_tasks()), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class BatchToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patches = [
            patch.object(server, "NETWORK", RequestPolicy(fetch_rpm=0, search_rpm=0)),
            patch.object(server.httpx, "AsyncClient", side_effect=lambda **kw: REAL_CLIENT(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, text="fixture")), trust_env=False)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_fetch_partial_order_duplicates_and_no_late_jobs(self):
        calls, cleaned = [], []
        async def fetch(client, url, max_chars, **options):
            calls.append(url)
            if url.endswith("fast"):
                return page(url)
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(url)
        urls = ["https://example.org/fast"] + [f"https://example.org/{n}" for n in range(7)]
        with patch.object(server, "_fetch_one", side_effect=fetch):
            data = json.loads(await server.fetch_pages(urls + [urls[0]], time_budget_seconds=0.05, response_format="json"))
        self.assertEqual([p["url"] for p in data["pages"]], urls + [urls[0]])
        self.assertEqual(data["successful"], 2)
        self.assertEqual(data["batch"]["completed"], 1)
        self.assertEqual(data["batch"]["timed_out"], 5)
        self.assertEqual(data["batch"]["not_started"], 2)
        self.assertEqual(data["unfinished_urls"], urls[1:])
        self.assertFalse(data["pages"][-2]["started"])
        self.assertCountEqual(cleaned, urls[1:6])
        self.assertEqual(len(calls), 6)

    async def test_unexpected_fetch_failure_isolated(self):
        async def fetch(client, url, max_chars, **options):
            if url.endswith("bad"):
                raise RuntimeError("unexpected")
            return page(url)
        with patch.object(server, "_fetch_one", side_effect=fetch):
            data = json.loads(await server.fetch_pages(["https://a.org/bad", "https://a.org/good"], response_format="json"))
        self.assertEqual(data["successful"], 1)
        self.assertEqual(data["pages"][0]["error_code"], "fetch_error")
        self.assertTrue(data["batch"]["complete"])

    async def test_merge_timeout_keeps_finished_engine_results_and_closes_sibling(self):
        cleaned = asyncio.Event()
        async def slow_bing(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        with patch.object(server, "_ddg", AsyncMock(return_value=[result("https://example.org/a")])), \
                patch.object(server, "_bing", side_effect=slow_bing):
            data = json.loads(await server.web_search_multi(["q"], strategy="merge", time_budget_seconds=0.05, response_format="json"))
        self.assertEqual(len(data["results"]), 1)
        self.assertTrue(data["ok"])
        self.assertFalse(data["complete"])
        self.assertEqual(data["queries"][0]["search_status"], "results")
        self.assertEqual([a["status"] for a in data["queries"][0]["attempts"]], ["success", "deadline"])
        self.assertTrue(cleaned.is_set())

    async def test_unstarted_queries_are_not_reported_as_no_results(self):
        async def search(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(server, "_search", side_effect=search):
            data = json.loads(await server.web_search_multi([str(n) for n in range(7)], time_budget_seconds=0.03, response_format="json"))
        self.assertEqual(data["batch"]["not_started"], 2)
        self.assertTrue(all(q["search_status"] == "incomplete" for q in data["queries"]))
        self.assertIn("尚未开始", data["queries"][-1]["diagnostics"][0])

    async def test_research_retains_search_sources_when_search_uses_entire_budget(self):
        async def slow(*args):
            await asyncio.Event().wait()
        with patch.object(server, "_ddg", AsyncMock(return_value=[result("https://example.org/a")])), \
                patch.object(server, "_bing", side_effect=slow), \
                patch.object(server, "_fetch_one", AsyncMock()) as fetch:
            data = json.loads(await server.deep_research("q", strategy="merge", time_budget_seconds=0.05, response_format="json"))
        fetch.assert_not_called()
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["unfinished_urls"], ["https://example.org/a"])
        self.assertEqual(data["pages"][0]["error_code"], "batch_deadline")
        self.assertFalse(data["pages"][0]["started"])
        self.assertEqual(data["batch"]["timed_out"], 1)
        self.assertEqual(data["batch"]["not_started"], 1)

    async def test_research_partial_bodies_still_obey_character_budget(self):
        urls = ["https://example.org/fast", "https://example.org/slow"]
        async def fetch(client, url, max_chars):
            if url.endswith("fast"):
                return page(url)
            await asyncio.Event().wait()
        with patch.object(server, "_search", AsyncMock(return_value=server.SearchOutcome([result(u) for u in urls]))), \
                patch.object(server, "_fetch_one", side_effect=fetch):
            data = json.loads(await server.deep_research("q", time_budget_seconds=0.04, max_total_chars=5, response_format="json"))
        self.assertEqual(data["pages"][0]["text"], "Compl")
        self.assertEqual(data["pages"][0]["next_index"], 5)
        self.assertEqual(data["returned_body_chars"], 5)
        self.assertEqual(data["successful_pages"], 1)
        self.assertEqual(data["unfinished_urls"], [urls[1]])
        self.assertEqual(data["batch"]["completed"], 2)  # Search + first page.

    async def test_invalid_budget_never_sends_requests(self):
        with patch.object(server.httpx, "AsyncClient") as client:
            for tool, args in [(server.fetch_pages, {"urls": ["https://example.org"]}),
                               (server.web_search_multi, {"queries": ["q"]}),
                               (server.deep_research, {"query": "q"})]:
                data = json.loads(await tool(**args, time_budget_seconds=0, response_format="json"))
                self.assertFalse(data["ok"])
                self.assertIn("time_budget_seconds", data["error"])
        client.assert_not_called()

    async def test_fetch_timeout_cleans_shared_inflight_and_keeps_completed_cache(self):
        async def uncached(client, url):
            if url.endswith("fast"):
                return page(url)
            await asyncio.Event().wait()
        with patch.object(server, "_fetch_uncached", side_effect=uncached):
            data = json.loads(await server.fetch_pages(["https://example.org/fast", "https://example.org/slow"],
                                                       time_budget_seconds=0.03, response_format="json"))
        self.assertEqual(data["successful"], 1)
        self.assertEqual(server.NETWORK.inflight, {})
        self.assertIsNone(server.NETWORK.cache.get("https://example.org/slow"))
        self.assertIsNotNone(server.NETWORK.cache.get("https://example.org/fast"))

    async def test_timed_out_follower_does_not_cancel_another_calls_download(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def uncached(client, url):
            entered.set()
            await release.wait()
            return page(url)
        url = "https://example.org/shared"
        with patch.object(server, "_fetch_uncached", side_effect=uncached):
            async with REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200)), trust_env=False) as client:
                owner = asyncio.create_task(server._fetch_one(client, url, 100))
                await entered.wait()
                data = json.loads(await server.fetch_pages([url], time_budget_seconds=0.03, response_format="json"))
                self.assertFalse(owner.done())
                release.set()
                result_page = await owner
        self.assertTrue(result_page["ok"])
        self.assertEqual(data["pages"][0]["error_code"], "batch_deadline")
        self.assertEqual(server.NETWORK.inflight, {})
