import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from mcp_harries_resourcer import server
from mcp_harries_resourcer.parse_policy import ParseFailure, ParsePolicy


FIXTURES = Path(__file__).parent / "fixtures"
REAL_CLIENT = httpx.AsyncClient


class SearchParsingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / "started"
        self.hosts = []
        def respond(request):
            self.hosts.append(request.url.host)
            engine = "ddg" if "duckduckgo" in request.url.host else "bing"
            return httpx.Response(200, text=self.html(engine))
        client = patch.object(server.httpx, "AsyncClient", side_effect=lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(respond), trust_env=False, **kw))
        network = patch.object(server, "NETWORK", server.RequestPolicy(search_rpm=0, fetch_rpm=0))
        for patcher in (client, network):
            patcher.start()
            self.addCleanup(patcher.stop)

    def html(self, engine):
        return (FIXTURES / f"{engine}_results.html").read_text(encoding="utf-8")

    def policy(self, mode="busy", **options):
        return ParsePolicy(command=[sys.executable, str(FIXTURES / "parse_process.py"),
                                    mode, str(self.marker)], **options)

    async def started(self):
        async def poll():
            while not self.marker.exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), 10)

    async def test_real_worker_returns_both_engines_and_parse_metadata(self):
        policy = ParsePolicy()
        for engine, expected in (("ddg", "A search fixture."), ("bing", "Readable snippet.")):
            result = await policy.parse_search(self.html(engine), engine, 1)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(result["items"][0]["snippet"], expected)
            self.assertEqual(result["parse_info"]["mode"], "subprocess")
            self.assertFalse(policy.active)

    async def test_cpu_search_remains_cancellable_without_starting_fallback(self):
        policy = self.policy()
        with patch.object(server, "PARSER", policy):
            task = asyncio.create_task(server._search("python", 1))
            try:
                await self.started()
                children = list(policy.active)
                # A worker spins continuously; the caller must still run.
                for _ in range(5):
                    await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertEqual(len(children), 1)
            finally:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(self.hosts, ["html.duckduckgo.com"])
        self.assertTrue(all(p.returncode is not None for p in children))
        self.assertFalse(policy.active)

    async def test_article_and_search_share_slots_and_queued_cancel_does_not_spawn(self):
        policy = self.policy(concurrency=1)
        article = asyncio.create_task(policy.parse("body", "url"))
        search = None
        try:
            await self.started()
            child, = policy.active
            search = asyncio.create_task(policy.parse_search(self.html("bing"), "bing", 1))
            await asyncio.sleep(0.05)
            self.assertEqual(policy.active, {child})
            self.assertFalse(search.done())
            search.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await search
            self.assertIsNone(child.returncode)
        finally:
            tasks = [article] + ([search] if search else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertFalse(policy.active)
        policy.command[-2] = "echo"
        recovered = await policy.parse_search(self.html("bing"), "bing", 1)
        self.assertEqual(recovered["items"][0]["title"], "Example guide")

    async def test_parse_deadline_falls_back_with_code_and_retains_result(self):
        policy = self.policy("search_busy", timeout=2)
        with patch.object(server, "PARSER", policy):
            result = json.loads(await server.web_search("python", 1, response_format="json"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["attempts"][0]["error_code"], "parse_deadline")
        self.assertEqual(result["attempts"][1]["status"], "success")
        self.assertEqual(result["results"][0]["title"], "Example guide")
        self.assertFalse(policy.active)

    async def test_search_queue_timeout_never_spawns(self):
        policy = self.policy(timeout=0.05)
        for _ in range(2):
            await policy.slots.acquire()
        try:
            with self.assertRaises(ParseFailure) as caught:
                await policy.parse_search(self.html("bing"), "bing", 1)
            self.assertEqual(caught.exception.code, "parse_deadline")
            self.assertFalse(self.marker.exists())
        finally:
            for _ in range(2):
                policy.slots.release()

    async def test_search_protocol_failures_are_reaped_and_recover(self):
        for mode, expected in (("crash", "parse_worker_failed"), ("invalid", "parse_worker_failed"),
                               ("oversize", "parse_output_too_large")):
            with self.subTest(mode=mode):
                policy = self.policy(mode, max_output_bytes=1024)
                with self.assertRaises(ParseFailure) as caught:
                    await policy.parse_search(self.html("bing"), "bing", 1)
                self.assertEqual(caught.exception.code, expected)
                self.assertFalse(policy.active)
                policy.command[-2] = "echo"
                self.assertTrue((await policy.parse_search(self.html("bing"), "bing", 1))["ok"])

    async def test_worker_schema_is_checked_before_results_are_used(self):
        item = {"url": "https://example.org", "title": "Title", "snippet": "", "source": "bing"}
        for result in (
            {"ok": True, "items": [item, item], "error": ""},
            {"ok": True, "items": [{**item, "source": "ddg"}], "error": ""},
            {"ok": True, "items": [{**item, "title": None}], "error": ""},
            {"ok": False, "items": [], "error": ""},
            {"ok": False, "items": [item], "error": "failed"},
            {"ok": True, "items": [item], "error": "", "encoding_info": {
                "encoding": "utf-8", "source": [], "had_errors": False}},
        ):
            with self.subTest(result=result):
                policy = self.policy("json")
                with self.assertRaises(ParseFailure) as caught:
                    await policy.parse_search(json.dumps(result), "bing", 1)
                self.assertEqual(caught.exception.code, "parse_worker_failed")
                self.assertFalse(policy.active)

    async def test_repeated_cancel_during_search_spawn_reaps_child(self):
        policy = self.policy()
        spawned, release = asyncio.Event(), asyncio.Event()
        original = asyncio.create_subprocess_exec
        children = []
        async def delayed(*args, **kwargs):
            child = await original(*args, **kwargs)
            children.append(child)
            spawned.set()
            await release.wait()
            return child
        with patch.object(server, "PARSER", policy), patch(
                "mcp_harries_resourcer.parse_policy.asyncio.create_subprocess_exec", side_effect=delayed):
            task = asyncio.create_task(server._search("python", 1))
            try:
                await asyncio.wait_for(spawned.wait(), 10)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertTrue(all(p.returncode is not None for p in children))
        self.assertFalse(policy.active)
