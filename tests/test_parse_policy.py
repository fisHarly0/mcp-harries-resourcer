import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from parse_policy import ParseFailure, ParsePolicy
from request_policy import RequestPolicy
import server


class ParserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / "started"

    def policy(self, mode="busy", **options):
        return ParsePolicy(command=[sys.executable, str(Path(__file__).parent / "fixtures" / "parse_process.py"),
                                    mode, str(self.marker)], **options)

    async def started(self):
        async def poll():
            while not self.marker.exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), 10)

    async def test_actual_worker_extracts_unicode_and_code_without_changing_cwd(self):
        html = (Path(__file__).parent / "fixtures" / "technical_article.html").read_text(encoding="utf-8")
        policy = ParsePolicy()
        result = await policy.parse(html, "https://example.org/docs/guides/start.html")
        self.assertTrue(result["ok"])
        self.assertIn('"甲", "乙"', result["_markdown"])
        self.assertEqual(result["parse_info"]["mode"], "subprocess")
        self.assertEqual(policy.active, set())

    async def test_cpu_work_does_not_block_loop_and_cancel_reaps_child(self):
        policy = self.policy()
        task = asyncio.create_task(policy.parse("body", "https://example.org"))
        try:
            await self.started()
            children = list(policy.active)
            self.assertEqual(len(children), 1)
            self.assertEqual(int(self.marker.read_text(encoding="utf-8")), children[0].pid)
            # The fixture spins without sleeps. This task must still run.
            for _ in range(5):
                await asyncio.sleep(0.01)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(all(child.returncode is not None for child in children))
        self.assertFalse(policy.active)

    async def test_deadline_kills_worker_and_slot_can_be_reused(self):
        policy = self.policy(timeout=2, concurrency=1)
        task = asyncio.create_task(policy.parse("body", "url"))
        await self.started()
        children = list(policy.active)
        with self.assertRaises(ParseFailure) as caught:
            await task
        self.assertEqual(caught.exception.code, "parse_deadline")
        self.assertTrue(all(child.returncode is not None for child in children))
        policy.command[-2] = "echo"
        self.assertEqual((await policy.parse("next", "url"))["text"], "next")

    async def test_waiting_for_slot_uses_same_deadline_without_spawning(self):
        policy = self.policy(timeout=0.05, concurrency=1)
        await policy.slots.acquire()
        with self.assertRaises(ParseFailure) as caught:
            await policy.parse("body", "url")
        self.assertEqual(caught.exception.code, "parse_deadline")
        self.assertFalse(self.marker.exists())
        policy.slots.release()

    async def test_concurrent_call_cancellation_leaves_other_child_running(self):
        policy = self.policy(concurrency=2)
        tasks = [asyncio.create_task(policy.parse("body", "url")) for _ in range(3)]
        try:
            await self.started()
            async def two_children():
                while len(policy.active) < 2:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(two_children(), 10)
            self.assertEqual(len(policy.active), 2)
            tasks[2].cancel()  # Queued job must not acquire a child later.
            await asyncio.gather(tasks[2], return_exceptions=True)
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            self.assertFalse(tasks[1].done())
            self.assertEqual(len(policy.active), 1)
        finally:
            children = list(policy.active)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertFalse(policy.active)
        self.assertTrue(all(child.returncode is not None for child in children))

    async def _check_spawn_cancel(self, through_fetch=False):
        policy = self.policy()
        created, release = asyncio.Event(), asyncio.Event()
        original = asyncio.create_subprocess_exec
        children = []
        async def delayed_spawn(*args, **kwargs):
            child = await original(*args, **kwargs)
            children.append(child)
            created.set()
            await release.wait()
            return child
        async def fetch():
            with patch.object(server, "PARSER", policy), patch.object(server, "NETWORK", RequestPolicy(fetch_rpm=0)):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="body"))) as client:
                    return await server._fetch_one(client, "https://example.org", 10)
        with patch("parse_policy.asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            task = asyncio.create_task(fetch() if through_fetch else policy.parse("body", "url"))
            await asyncio.wait_for(created.wait(), 10)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(all(child.returncode is not None for child in children))
        self.assertFalse(policy.active)

    async def test_cancel_during_spawn_and_repeated_cancel_still_reaps_child(self):
        await self._check_spawn_cancel()

    async def test_early_timeout_wrapper_cancel_still_waits_for_child(self):
        async def early_cancel(awaitable, timeout):
            task = asyncio.ensure_future(awaitable)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                task.cancel()
                raise  # Simulate 3.10 wait_for escaping before child cleanup.
        with patch("parse_policy.asyncio.wait_for", side_effect=early_cancel):
            await self._check_spawn_cancel()
            await self._check_spawn_cancel(through_fetch=True)

    async def test_crash_invalid_and_oversize_output_are_failures(self):
        for mode, code in [("crash", "parse_worker_failed"), ("invalid", "parse_worker_failed"),
                           ("oversize", "parse_output_too_large")]:
            with self.subTest(mode=mode):
                policy = self.policy(mode, max_output_bytes=1024)
                with self.assertRaises(ParseFailure) as caught:
                    await policy.parse("body", "url")
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(policy.active)

    async def test_spawn_failure_is_reported(self):
        policy = ParsePolicy(command=[str(Path(self.tmp.name) / "missing-executable")])
        with self.assertRaises(ParseFailure) as caught:
            await policy.parse("body", "url")
        self.assertEqual(caught.exception.code, "parse_worker_failed")
        self.assertFalse(policy.active)

    async def test_batch_deadline_reaps_parser_but_keeps_cached_page(self):
        policy = self.policy(timeout=20)
        network = RequestPolicy(fetch_rpm=0)
        network.cache.put("https://example.org/fast", {"ok": True, "text": "finished", "title": "Fast"})
        original = httpx.AsyncClient
        factory = lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="body")))
        with patch.object(server, "PARSER", policy), patch.object(server, "NETWORK", network), \
                patch.object(server.httpx, "AsyncClient", side_effect=factory):
            task = asyncio.create_task(server.fetch_pages(
                ["https://example.org/fast", "https://example.org/slow"], time_budget_seconds=2, response_format="json"))
            await self.started()
            children = list(policy.active)
            result = json.loads(await task)
        self.assertEqual(result["pages"][0]["text"], "finished")
        self.assertEqual(result["pages"][1]["error_code"], "batch_deadline")
        self.assertTrue(all(child.returncode is not None for child in children))
        self.assertFalse(policy.active)
        self.assertFalse(network.inflight)
        self.assertIsNone(network.cache.get("https://example.org/slow"))

    async def test_fetch_timeout_never_caches_partial_parse(self):
        policy = self.policy(timeout=2)
        network = RequestPolicy(fetch_rpm=0)
        with patch.object(server, "PARSER", policy), patch.object(server, "NETWORK", network):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="body"))) as client:
                result = await server._fetch_one(client, "https://example.org", 10)
                self.assertEqual(result["error_code"], "parse_deadline")
                self.assertFalse(network.cache.entries)
                self.assertFalse(network.inflight)
                policy.command[-2] = "echo"
                recovered = await server._fetch_one(client, "https://example.org", 10)
                self.assertTrue(recovered["ok"])
                self.assertFalse(recovered["cached"])


class ParserSettingsTests(unittest.TestCase):
    def test_environment_bounds(self):
        with patch.dict(os.environ, {"RESOURCER_PARSE_CONCURRENCY": "0", "RESOURCER_PARSE_TIMEOUT_SECONDS": "999"}):
            policy = ParsePolicy.from_env()
        self.assertEqual(policy.timeout, 20)
        self.assertEqual(policy.slots._value, 2)
