import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from mcp_harries_resourcer import server
from mcp_harries_resourcer.http_policy import HTTPPolicy


FIXTURES = Path(__file__).parent / "fixtures"
REAL_CLIENT = httpx.AsyncClient


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def response(body="", status=200):
    return httpx.Response(status, text=body, request=httpx.Request("GET", "https://example.org"))


def item(url="https://example.org/guide"):
    return {"url": url, "title": "Guide", "snippet": "A useful article.", "source": "ddg"}


class ParserTests(unittest.IsolatedAsyncioTestCase):
    def test_ddg_urls_snippets_and_deduplication(self):
        items = server._parse_ddg(fixture("ddg_results.html"), 10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["url"], "https://example.org/guide?q=a%20b")
        self.assertEqual(items[0]["snippet"], "A search fixture.")

    def test_bing_redirect_and_deduplication(self):
        items = server._parse_bing(fixture("bing_results.html"), 10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["url"], "https://example.org/guide")
        self.assertEqual(items[0]["snippet"], "Readable snippet.")

    def test_inline_search_highlights_preserve_word_boundaries(self):
        for parser, template in (
            (server._parse_bing, '<li class="b_algo"><h2><a href="https://example.org">{title}</a></h2><p>{snippet}</p></li>'),
            (server._parse_ddg, '<div class="result"><a class="result__a" href="https://example.org">{title}</a><div class="result__snippet">{snippet}</div></div>'),
        ):
            for title, expected in (
                ('<b>Python</b> 3.14 <b>documentation</b>', 'Python 3.14 documentation'),
                ('<b>use</b>Effect', 'useEffect'),
                ('<b>异步</b>编程', '异步编程'),
                ('first<br>second', 'first second'),
            ):
                with self.subTest(parser=parser.__name__, title=title):
                    result = parser(template.format(title=title, snippet='Read <b>this</b>.'), 1)[0]
                    self.assertEqual(result['title'], expected)
                    self.assertEqual(result['snippet'], 'Read this.')

    async def test_result_limit(self):
        for engine in ("ddg", "bing"):
            with self.subTest(engine=engine):
                self.assertEqual(len(await server._parse_search_response(response(fixture(f"{engine}_results.html")), engine, 1)), 1)

    async def test_explicit_empty_pages(self):
        for engine in ("ddg", "bing"):
            with self.subTest(engine=engine):
                self.assertEqual(await server._parse_search_response(response(fixture(f"{engine}_empty.html")), engine, 5), [])

    async def test_unrecognized_html_is_not_empty_results(self):
        for engine in ("ddg", "bing"):
            with self.subTest(engine=engine):
                with self.assertRaisesRegex(server.SearchEngineError, "解析失败"):
                    await server._parse_search_response(response(fixture("layout_changed.html")), engine, 5)

    async def test_rate_limit_is_reported(self):
        with self.assertRaisesRegex(server.SearchEngineError, "限流"):
            await server._parse_search_response(response(status=429), "ddg", 5)

    async def test_challenge_is_reported(self):
        with self.assertRaisesRegex(server.SearchEngineError, "验证码"):
            await server._parse_search_response(response(fixture("ddg_challenge.html")), "ddg", 5)

    async def test_http_errors_are_not_empty_results(self):
        with self.assertRaises(httpx.HTTPStatusError):
            await server._parse_search_response(response(status=503), "bing", 5)

    async def test_malformed_result_url_is_a_parse_failure(self):
        html = '<div class="result"><a class="result__a" href="https://[bad">Broken</a></div>'
        with self.assertRaisesRegex(server.SearchEngineError, "解析失败"):
            await server._parse_search_response(response(html), "ddg", 5)


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        policy = patch.object(server, "NETWORK", server.RequestPolicy(
            search_rpm=0, fetch_rpm=0, http_policy=HTTPPolicy(retries=0)))
        policy.start()
        self.addCleanup(policy.stop)

    def client_patch(self, handler):
        return patch.object(server.httpx, "AsyncClient", return_value=REAL_CLIENT(transport=httpx.MockTransport(handler), follow_redirects=True))

    async def test_bing_homepage_redirect_recovers_search_route(self):
        routes = []
        def handler(request):
            routes.append((request.url.host, request.url.path))
            if request.url.host == "cn.bing.com":
                return httpx.Response(301, headers={"Location": "https://www.bing.com/?q=python"})
            if request.url.path == "/":
                return httpx.Response(200, text="<html>Bing homepage</html>")
            self.assertEqual(request.url.params["q"], "Python 中文")
            return httpx.Response(200, text=fixture("bing_results.html"))
        with self.client_patch(handler):
            text = await server.web_search("Python 中文")
        self.assertEqual(routes, [("cn.bing.com", "/search"), ("www.bing.com", "/"), ("www.bing.com", "/search")])
        self.assertIn("Example guide", text)
        self.assertNotIn("搜索未完成", text)

    async def test_rate_limit_falls_back_and_keeps_diagnostic(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            if "duckduckgo" in request.url.host:
                return httpx.Response(429)
            return httpx.Response(200, text=fixture("bing_results.html"))
        with self.client_patch(handler):
            text = await server.web_search("python", 1)
        self.assertEqual(hosts, ["html.duckduckgo.com", "cn.bing.com"])
        self.assertIn("Example guide", text)
        self.assertIn("限流", text)

    async def test_chinese_prefers_bing(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(200, text=fixture("bing_results.html"))
        with self.client_patch(handler):
            text = await server.web_search("中文资料")
        self.assertEqual(hosts, ["cn.bing.com"])
        self.assertIn("Example guide", text)

    async def test_all_timeouts_are_not_no_results(self):
        def handler(request):
            raise httpx.ReadTimeout("timeout", request=request)
        with self.client_patch(handler):
            text = await server.web_search("python")
        self.assertIn("搜索未完成", text)
        self.assertIn("超时", text)
        self.assertNotIn("未找到", text)

    async def test_network_errors_are_reported(self):
        def handler(request):
            raise httpx.ConnectError("offline", request=request)
        with self.client_patch(handler):
            text = await server.web_search("python")
        self.assertIn("网络连接失败", text)

    async def test_all_explicit_empty_pages(self):
        def handler(request):
            engine = "ddg" if "duckduckgo" in request.url.host else "bing"
            return httpx.Response(200, text=fixture(f"{engine}_empty.html"))
        with self.client_patch(handler):
            text = await server.web_search("no matches")
        self.assertIn("未找到", text)
        self.assertNotIn("搜索未完成", text)

    async def test_empty_plus_failed_engine_is_inconclusive(self):
        def handler(request):
            if "duckduckgo" in request.url.host:
                return httpx.Response(200, text=fixture("ddg_empty.html"))
            return httpx.Response(503)
        with self.client_patch(handler):
            text = await server.web_search("uncertain")
        self.assertIn("搜索未完成", text)
        self.assertIn("HTTP 503", text)

    async def test_unknown_layout_is_reported(self):
        with self.client_patch(lambda request: httpx.Response(200, text=fixture("layout_changed.html"))):
            text = await server.web_search("python")
        self.assertIn("解析失败", text)
        self.assertNotIn("未找到", text)

    async def test_blank_query_does_not_send_requests(self):
        with patch.object(server.httpx, "AsyncClient") as client:
            text = await server.web_search("  ")
        client.assert_not_called()
        self.assertIn("关键词不能为空", text)

    async def test_multi_search_dedup_is_not_no_results(self):
        with patch.object(server, "_search", AsyncMock(return_value=server.SearchOutcome([item()]))):
            text = await server.web_search_multi(["first", "second"])
        self.assertIn("去重后 1 条", text)
        self.assertIn("已全部去重", text)
        self.assertNotIn("未找到", text)

    async def test_multi_search_preserves_failed_query(self):
        outcomes = [server.SearchOutcome([item()]), server.SearchOutcome(failures=["Bing：限流"])]
        with patch.object(server, "_search", AsyncMock(side_effect=outcomes)):
            text = await server.web_search_multi(["first", "second"])
        self.assertIn("Guide", text)
        self.assertIn("搜索未完成", text)


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        policy = patch.object(server, "NETWORK", server.RequestPolicy(search_rpm=0, fetch_rpm=0))
        policy.start()
        self.addCleanup(policy.stop)

    async def test_fetch_parser_failure_is_a_result(self):
        async with REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>bad</html>"))) as client:
            with patch.object(server.PARSER, "parse", AsyncMock(side_effect=server.ParseFailure("parse_worker_failed", "正文解析失败"))):
                result = await server._fetch_one(client, "https://example.org", 100)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "正文解析失败")

    async def test_deep_research_marks_actual_fetch_failures(self):
        fetched = [
            {"url": "https://example.org/guide", "ok": True, "title": "Guide", "text": "Body", "truncated": False},
            {"url": "https://example.org/broken", "ok": False, "error": "HTTP 403", "title": "", "text": ""},
        ]
        with patch.object(server, "_search", AsyncMock(return_value=server.SearchOutcome([item(), item("https://example.org/broken")]))):
            with patch.object(server, "_fetch_one", AsyncMock(side_effect=fetched)):
                text = await server.deep_research("example")
        self.assertIn("成功 1 篇", text)
        self.assertIn("❌ 2.", text)
        self.assertIn("HTTP 403", text)

    async def test_deep_research_preserves_search_failure(self):
        with patch.object(server, "_search", AsyncMock(return_value=server.SearchOutcome(failures=["DuckDuckGo：限流"]))):
            text = await server.deep_research("example")
        self.assertIn("搜索未完成", text)

    async def test_deep_research_zero_fetch(self):
        with patch.object(server, "_search", AsyncMock(return_value=server.SearchOutcome([item()]))):
            with patch.object(server, "_fetch_one", AsyncMock()) as fetch:
                text = await server.deep_research("example", fetch_top_n=0)
        fetch.assert_not_called()
        self.assertIn("计划抓取 0 篇", text)

    async def test_batch_fetch_limits_concurrency_and_keeps_errors(self):
        active = peak = 0
        async def fetch(client, url, max_chars, **options):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return {"url": url, "ok": False, "error": "HTTP 403", "title": "", "text": ""}
        with patch.object(server, "_fetch_one", side_effect=fetch):
            text = await server.fetch_pages([f"https://example.org/{i}" for i in range(12)])
        self.assertLessEqual(peak, 5)
        self.assertIn("0/12 成功", text)


class SaveTests(unittest.TestCase):
    def test_same_second_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "RESEARCH_ROOT", Path(tmp)), patch.object(server, "datetime") as clock:
                clock.now.return_value = datetime(2026, 10, 4, 12, 0, 0)
                server.save_finding("demo", "same", "first body")
                server.save_finding("demo", "same", "second body")
            saved = list((Path(tmp) / "demo").glob("*.md"))
            self.assertEqual(len(saved), 2)
            bodies = [p.read_text(encoding="utf-8") for p in saved]
            self.assertTrue(any("first body" in b for b in bodies))
            self.assertTrue(any("second body" in b for b in bodies))

    def test_metadata_quotes_and_local_search(self):
        title = 'Notes: "MCP"\nsecond line'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "RESEARCH_ROOT", Path(tmp)):
                server.save_finding("demo", title, "A searchable research finding.", tags="web: search, MCP")
            saved = next((Path(tmp) / "demo").glob("*.md"))
            text = saved.read_text(encoding="utf-8")
            self.assertEqual(json.loads(text.splitlines()[1].split(": ", 1)[1]), title)
            self.assertIn("命中 1 处", asyncio.run(server.search_local("searchable", tmp, include_ext="md")))


if __name__ == "__main__":
    unittest.main()
