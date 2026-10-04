import asyncio
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import server
from request_policy import RequestPolicy
from http_policy import HTTPPolicy


REAL_CLIENT = httpx.AsyncClient


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.article = "甲乙🙂丙丁中文 article body"
        self.status = 200
        def handler(request):
            self.requests.append(str(request.url))
            return httpx.Response(self.status, text="<html><title>Guide</title></html>")
        def client_factory(**kwargs):
            return REAL_CLIENT(transport=httpx.MockTransport(handler))
        patches = [
            patch.object(server, "NETWORK", RequestPolicy(search_rpm=0, fetch_rpm=0, http_policy=HTTPPolicy(retries=0))),
            patch.object(server.httpx, "AsyncClient", side_effect=client_factory),
            patch.object(server.trafilatura, "extract", side_effect=lambda *a, **k: self.article),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def read(self, **kwargs):
        return json.loads(await server.fetch_page("https://example.org/page", response_format="json", **kwargs))

    async def test_unicode_pages_reconstruct_article_without_redownload(self):
        parts = []
        offset = 0
        version = ""
        while True:
            result = await self.read(max_chars=3, start_index=offset, expected_content_id=version)
            self.assertTrue(result["ok"])
            self.assertEqual(result["start_index"], offset)
            self.assertEqual(result["total_chars"], len(self.article))
            self.assertLessEqual(len(result["text"]), 3)
            version = result["content_id"]
            parts.append(result["text"])
            if result["next_index"] is None:
                break
            offset = result["next_index"]
        self.assertEqual("".join(parts), self.article)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(version, hashlib.sha256(self.article.encode("utf-8")).hexdigest())

    async def test_zero_length_means_read_to_end_from_offset(self):
        result = await self.read(start_index=4, max_chars=0)
        self.assertEqual(result["text"], self.article[4:])
        self.assertIsNone(result["next_index"])

    async def test_out_of_range_offset_returns_end_not_fake_failure(self):
        result = await self.read(start_index=999)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["start_index"], len(self.article))
        self.assertIsNone(result["next_index"])

    async def test_negative_parameters_do_not_request(self):
        for kwargs in ({"start_index": -1}, {"max_chars": -1}):
            with self.subTest(kwargs=kwargs):
                result = await self.read(**kwargs)
                self.assertFalse(result["ok"])
        self.assertEqual(self.requests, [])

    async def test_default_text_mode_contains_resume_arguments(self):
        result = await server.fetch_page("https://example.org/page", max_chars=3)
        self.assertIn(self.article[:3], result)
        self.assertIn("start_index=3", result)
        self.assertIn("expected_content_id=", result)

    async def test_refresh_updates_content_and_cache(self):
        original = await self.read()
        self.article = "New content"
        cached = await self.read()
        refreshed = await self.read(refresh=True)
        after = await self.read()
        self.assertEqual(cached["text"], original["text"])
        self.assertEqual(refreshed["text"], "New content")
        self.assertFalse(refreshed["cached"])
        self.assertEqual(after["text"], "New content")
        self.assertTrue(after["cached"])
        self.assertNotEqual(original["content_id"], refreshed["content_id"])
        self.assertEqual(len(self.requests), 2)

    async def test_refresh_failure_does_not_serve_old_content_as_fresh(self):
        await self.read()
        self.status = 503
        refreshed = await self.read(refresh=True)
        self.assertFalse(refreshed["ok"])
        self.assertEqual(refreshed["text"], "")
        self.status = 200
        self.article = "Recovered"
        result = await self.read()
        self.assertEqual(result["text"], "Recovered")
        self.assertEqual(len(self.requests), 3)

    async def test_version_mismatch_stops_reading(self):
        original = await self.read(max_chars=3)
        self.article = "A changed article with different offsets"
        result = await self.read(start_index=3, refresh=True, expected_content_id=original["content_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertIn("版本已变化", result["error"])

    async def test_json_errors_are_parseable(self):
        self.status = 403
        result = await self.read()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HTTP 403")

    async def test_batch_json_has_per_page_resume_metadata(self):
        result = json.loads(await server.fetch_pages(["https://example.org/a", "https://example.org/b"], max_chars=2, response_format="json"))
        self.assertEqual(result["successful"], 2)
        self.assertTrue(all(p["next_index"] == 2 for p in result["pages"]))
        self.assertTrue(all(p["fetched_at"] and p["final_url"] and p["content_id"] for p in result["pages"]))

    async def test_batch_refresh_deduplicates_more_urls_than_concurrency(self):
        result = json.loads(await server.fetch_pages(["https://example.org/page"] * 12, refresh=True, response_format="json"))
        self.assertEqual(result["successful"], 12)
        self.assertEqual(len(result["pages"]), 12)
        self.assertEqual(len(self.requests), 1)

    async def test_deep_research_total_budget_and_zero_body_resume(self):
        items = [{"url": f"https://example.org/{n}", "title": f"Page {n}", "snippet": "", "source": "ddg"} for n in range(3)]
        with patch.object(server, "_search", AsyncMock(return_value=server.SearchOutcome(items))):
            result = json.loads(await server.deep_research("query", fetch_top_n=3, max_chars_each=5, max_total_chars=7, response_format="json"))
        self.assertEqual([len(p["text"]) for p in result["pages"]], [5, 2, 0])
        self.assertEqual(result["returned_body_chars"], 7)
        self.assertEqual(result["successful_pages"], 3)
        self.assertEqual(result["pages"][2]["next_index"], 0)
        continued = json.loads(await server.fetch_page(result["pages"][2]["url"], start_index=0, max_chars=0, response_format="json"))
        self.assertEqual(continued["text"], self.article)
        self.assertEqual(len(self.requests), 3)

    async def test_deep_research_unlimited_body(self):
        items = [{"url": "https://example.org/page", "title": "Guide", "snippet": "", "source": "ddg"}]
        with patch.object(server, "_search", AsyncMock(return_value=server.SearchOutcome(items))):
            result = json.loads(await server.deep_research("query", max_chars_each=0, max_total_chars=0, response_format="json"))
        self.assertEqual(result["pages"][0]["text"], self.article)
        self.assertIsNone(result["pages"][0]["next_index"])

    async def test_empty_search_and_failed_search_have_distinct_json_status(self):
        for outcome, status in [(server.SearchOutcome(), "no_results"), (server.SearchOutcome(failures=["timeout"]), "incomplete")]:
            with self.subTest(status=status), patch.object(server, "_search", AsyncMock(return_value=outcome)):
                result = json.loads(await server.deep_research("query", response_format="json"))
                self.assertEqual(result["search_status"], status)
        self.assertEqual(self.requests, [])


class RefreshConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_refreshes_share_download(self):
        policy = RequestPolicy(fetch_rpm=0)
        policy.cache.put("url", {"ok": True, "title": "Old", "text": "Old"})
        started, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def loader():
            nonlocal count
            count += 1
            started.set()
            await release.wait()
            return {"ok": True, "title": "New", "text": "New"}
        first = asyncio.create_task(policy.fetch("url", loader, refresh=True))
        await started.wait()
        second = asyncio.create_task(policy.fetch("url", loader, refresh=True))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(count, 1)
        self.assertTrue(all(r["text"] == "New" and not r["cached"] for r in results))
        self.assertEqual(policy.cache.get("url")["text"], "New")
