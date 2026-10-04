import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from mcp_harries_resourcer.progress import Progress, report, with_progress
from mcp_harries_resourcer import server


class Context:
    def __init__(self, token="test"):
        self.request_context = SimpleNamespace(meta=SimpleNamespace(progressToken=token))
        self.events = []

    async def report_progress(self, value, total=None, message=None):
        self.events.append((value, total, message))


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_coalesce_and_values_only_increase(self):
        ctx = Context()
        async with Progress(ctx, interval=0) as progress:
            for n in range(1000):
                progress.update(n, 1000, "update")
            progress.update(5, 1000, "old")
        self.assertEqual(ctx.events, [(999, 1000, "update")])
        self.assertFalse(any(t.get_name() == "resourcer-progress" for t in asyncio.all_tasks()))

    async def test_no_token_does_not_send_notifications(self):
        ctx = Context(None)
        @with_progress
        async def work(ctx=None):
            report(0, 1, "start")
            await asyncio.sleep(0)
            report(1, 1, "done")
        await work(ctx=ctx)
        self.assertEqual(ctx.events, [])

    async def test_transport_failure_or_blocked_notification_does_not_fail_work(self):
        for blocked in [False, True]:
            ctx = Context()
            async def fail(*args):
                if blocked:
                    await asyncio.Event().wait()
                raise RuntimeError("transport failed")
            ctx.report_progress = fail
            async with Progress(ctx, interval=0, timeout=0.02) as progress:
                progress.update(0, 1, "start")
                await asyncio.sleep(0)
                progress.update(1, 1, "done")
            self.assertTrue(progress.task.done())

    async def test_nested_calls_and_concurrent_requests_keep_their_context(self):
        @with_progress
        async def child(ctx=None):
            report(1, 2, "child")
            await asyncio.sleep(0)
        @with_progress
        async def parent(ctx=None):
            report(0, 2, "start")
            await asyncio.sleep(0)
            await child(ctx=ctx)
            report(2, 2, "done")
        contexts = [Context("a"), Context("b")]
        await asyncio.gather(*(parent(ctx=ctx) for ctx in contexts))
        for ctx in contexts:
            values = [event[0] for event in ctx.events]
            self.assertEqual(values, sorted(set(values)))
            self.assertEqual(values[-1], 2)

    async def test_cancellation_stops_sender_and_does_not_report_done(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        ctx = Context()
        async def send(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        ctx.report_progress = send
        @with_progress
        async def work(ctx=None):
            report(0, 1, "start")
            await asyncio.Event().wait()
        task = asyncio.create_task(work(ctx=ctx))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertFalse(any(t.get_name() == "resourcer-progress" for t in asyncio.all_tasks()))


class ToolProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        client = httpx.AsyncClient
        p = patch.object(server.httpx, "AsyncClient", side_effect=lambda **kw: client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200)), trust_env=False, verify=False))
        p.start()
        self.addCleanup(p.stop)

    async def test_duplicate_pages_count_once_and_errors_are_explicit(self):
        ctx = Context()
        async def fetch(client, url, *args, **kw):
            return {"ok": url.endswith("ok"), "url": url, "text": "", "error": "failed", "title": ""}
        with patch.object(server, "_fetch_one", side_effect=fetch):
            data = json.loads(await server.fetch_pages(["https://a/ok", "https://a/bad", "https://a/ok"],
                                                      response_format="json", ctx=ctx))
        self.assertEqual(data["successful"], 2)
        self.assertEqual(ctx.events[-1][:2], (2, 2))
        self.assertIn("成功 1，失败 1", ctx.events[-1][2])

    async def test_timeout_does_not_claim_unfinished_pages_completed(self):
        ctx = Context()
        async def fetch(client, url, *args, **kw):
            if url.endswith("slow"):
                await asyncio.Event().wait()
            return {"ok": True, "url": url, "text": "", "title": ""}
        with patch.object(server, "_fetch_one", side_effect=fetch):
            data = json.loads(await server.fetch_pages(["https://a/ok", "https://a/slow"],
                response_format="json", ctx=ctx, time_budget_seconds=0.15))
        self.assertTrue(data["batch"]["deadline_exceeded"])
        self.assertEqual(ctx.events[-1][:2], (1, 2))

    async def test_research_total_becomes_known_after_search(self):
        ctx = Context()
        async def search(*args, **kwargs):
            await asyncio.sleep(0.01)
            return server.SearchOutcome([{"url": "https://a/one", "title": "One", "snippet": ""}])
        async def fetch(client, url, *args, **kwargs):
            return {"ok": True, "url": url, "text": "body", "title": "One"}
        with patch.object(server, "_search", side_effect=search), patch.object(server, "_fetch_one", side_effect=fetch):
            result = json.loads(await server.deep_research("q", response_format="json", ctx=ctx))
        self.assertTrue(result["ok"])
        self.assertEqual(ctx.events[0][:2], (0, None))
        self.assertEqual(ctx.events[-1][:2], (2, 2))

    async def test_failed_query_counts_as_ended_with_failure(self):
        ctx = Context()
        outcomes = [server.SearchOutcome(), server.SearchOutcome(failures=["failed"]),
                    server.SearchOutcome([{"url": "https://a/one", "title": "One", "snippet": ""}], failures=["fallback used"])]
        with patch.object(server, "_search", AsyncMock(side_effect=outcomes)):
            data = json.loads(await server.web_search_multi(["one", "two", "three"], ctx=ctx, response_format="json"))
        self.assertEqual([q["ok"] for q in data["queries"]], [True, False, True])
        self.assertEqual(ctx.events[-1][:2], (3, 3))
        self.assertIn("成功 2，失败 1", ctx.events[-1][2])
