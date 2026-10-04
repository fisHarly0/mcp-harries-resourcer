"""Search backend contracts, including real HTTP and MCP without public engines."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_harries_resourcer import server
from mcp_harries_resourcer.client_pool import ClientPool
from mcp_harries_resourcer.http_policy import HTTPPolicy
from mcp_harries_resourcer.request_policy import RequestPolicy
from mcp_harries_resourcer.searxng import SearXNGError, parse_response, validate_backend
from scripts import search_smoke


def row(url="https://docs.example.org/guide", **extra):
    return {"url": url, "title": "Guide", "content": "Technical details", "engines": ["google", "bing"], **extra}


def response(data):
    return httpx.Response(200, json=data, request=httpx.Request("GET", "https://instance.example/search"))


class SearXNGProtocolTests(unittest.TestCase):
    def test_instance_paths_and_ipv6(self):
        for base, endpoint in [
            ("http://localhost:8080/", "http://localhost:8080/search"),
            ("https://instance.example/tools/", "https://instance.example/tools/search"),
            ("https://instance.example/tools/search/", "https://instance.example/tools/search"),
            ("http://[::1]:8080", "http://[::1]:8080/search"),
        ]:
            with self.subTest(base=base), patch.dict(os.environ, {"RESOURCER_SEARXNG_URL": base}):
                self.assertEqual(validate_backend("searxng", "fallback"), endpoint)

    def test_bad_configuration_is_rejected_without_echoing_values(self):
        for value in ["", "ftp://instance.example", "https://user:private@instance.example",
                      "https://instance.example?token=private", "https://instance.example/#private",
                      "https://instance.example:bad", "https://instance.example\\@evil.example",
                      "https://instance.example\n/path"]:
            with self.subTest(value=value), patch.dict(os.environ, {"RESOURCER_SEARXNG_URL": value}):
                with self.assertRaises(ValueError) as raised:
                    validate_backend("searxng", "fallback")
                self.assertNotIn("private", str(raised.exception))

    def test_builtin_ignores_optional_config_and_merge_is_explicit(self):
        with patch.dict(os.environ, {"RESOURCER_SEARXNG_URL": "invalid"}):
            self.assertIsNone(validate_backend("builtin", "merge"))
            with self.assertRaisesRegex(ValueError, "merge"):
                validate_backend("searxng", "merge")
            with self.assertRaisesRegex(ValueError, "backend"):
                validate_backend("unknown", "fallback")

    def test_malformed_json_never_means_no_results(self):
        for data in [None, [], {"error": "bad"}, {"results": None}, {"results": {}}]:
            with self.subTest(data=data), self.assertRaises(SearXNGError):
                parse_response(response(data))
        for body in (b"<html>Login</html>", b"[" * 2000):
            with self.assertRaises(SearXNGError):
                parse_response(httpx.Response(200, content=body, request=httpx.Request("GET", "https://instance.example")))

    def test_partial_invalid_rows_keep_original_rank_and_valid_content(self):
        reply = parse_response(response({"results": [None, {"title": "broken"}, row(content=None)],
                                         "unresponsive_engines": [["duckduckgo", "timeout"]]}))
        self.assertEqual(reply.invalid_results, 2)
        self.assertEqual(reply.items[0]["_rank"], 3)
        self.assertEqual(reply.items[0]["snippet"], "")
        self.assertEqual(reply.unresponsive_engines, [{"engine": "duckduckgo", "error": "timeout"}])
        self.assertEqual(len(reply.warnings), 2)

    def test_unrecognized_upstream_errors_are_incomplete(self):
        for errors in [None, {}, [None], [["engine", {}]]]:
            with self.subTest(errors=errors):
                reply = parse_response(response({"results": [], "unresponsive_engines": errors}))
                self.assertTrue(reply.warnings)

    def test_first_page_candidates_and_diagnostics_are_bounded(self):
        reply = parse_response(response({"results": [row()] * 200,
                                         "unresponsive_engines": [["x", "y"]] * 120}))
        self.assertEqual(len(reply.items), 50)
        self.assertEqual(len(reply.unresponsive_engines), 100)
        self.assertIn("截断", reply.warnings[-1])

    def test_benchmark_selects_instance_strategy_only(self):
        self.assertEqual(search_smoke.select_strategies("searxng"), ["fallback"])
        self.assertEqual(search_smoke.select_strategies("builtin"), ["fallback", "merge"])
        with self.assertRaises(ValueError):
            search_smoke.select_strategies("searxng", ["merge"])


class SearXNGSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.handler = lambda request: httpx.Response(200, json={"results": [row()]})

        async def transport(request):
            self.requests.append(request)
            reply = self.handler(request)
            return await reply if asyncio.iscoroutine(reply) else reply

        self.network = RequestPolicy(search_rpm=0, fetch_rpm=0, http_policy=HTTPPolicy(retries=0))
        patches = [patch.dict(os.environ, {"RESOURCER_SEARXNG_URL": "https://instance.example/prefix"}),
                   patch.object(server, "NETWORK", self.network),
                   patch.object(server, "HTTP_CLIENTS", ClientPool(lambda: httpx.AsyncClient(
                       transport=httpx.MockTransport(transport), trust_env=False)))]
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    async def call(self, **kwargs):
        return json.loads(await server.web_search("中文 & a+b", backend="searxng", response_format="json", **kwargs))

    async def test_local_filters_dedup_ranks_and_exact_query_encoding(self):
        self.handler = lambda request: response({"results": [
            row("https://example.org.evil.test/"), row("https://docs.example.org/guide#one"),
            row("https://blocked.example.org/"), row("https://DOCS.example.org:443/guide#two")],
            "unresponsive_engines": []})
        with patch.object(server, "_bing", AsyncMock()) as bing, patch.object(server, "_ddg", AsyncMock()) as ddg:
            data = await self.call(include_domains=["example.org"], exclude_domains=["blocked.example.org"])
        bing.assert_not_called()
        ddg.assert_not_called()
        self.assertTrue(data["complete"])
        self.assertEqual(data["backend"], "searxng")
        self.assertEqual(data["effective_strategy"], "instance")
        self.assertEqual(data["filtered_count"], 2)
        self.assertEqual(len(data["results"]), 1)
        provenance = data["results"][0]["provenance"]
        self.assertEqual([p["rank"] for p in provenance], [2, 4])
        self.assertEqual(provenance[0]["upstream_engines"], ["google", "bing"])
        self.assertNotIn("_rank", data["results"][0])
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/prefix/search")
        self.assertEqual(dict(request.url.params), {"q": "site:example.org -site:blocked.example.org 中文 & a+b",
                                                   "format": "json", "categories": "general", "pageno": "1"})

    async def test_partial_and_empty_failure_are_distinguished(self):
        for rows, ok, status in [([row()], True, "results"), ([], False, "incomplete")]:
            self.handler = lambda request: response({"results": rows, "unresponsive_engines": [["bing", "timeout"]]})
            data = await self.call()
            self.assertEqual(data["ok"], ok)
            self.assertEqual(data["search_status"], status)
            self.assertFalse(data["complete"])
            self.assertEqual(data["attempts"][0]["status"], "partial")
            self.assertIn("timeout", data["diagnostics"][0])
        self.handler = lambda request: response({"results": [], "unresponsive_engines": []})
        data = await self.call()
        self.assertEqual(data["search_status"], "no_results")
        self.assertTrue(data["complete"])

    async def test_filtered_empty_is_not_no_results(self):
        data = await self.call(include_domains=["other.example"])
        self.assertEqual(data["search_status"], "filtered_empty")
        self.assertTrue(data["complete"])

    async def test_config_errors_on_all_four_tools_do_not_send(self):
        with patch.dict(os.environ, {"RESOURCER_SEARXNG_URL": ""}):
            for tool, args in [(server.web_search, {"query": "q"}), (server.web_search_multi, {"queries": ["q"]}),
                               (server.search_chinese, {"query": "q"}), (server.deep_research, {"query": "q"})]:
                data = json.loads(await tool(**args, backend="searxng", response_format="json"))
                self.assertIn("RESOURCER_SEARXNG_URL", data["error"])
                self.assertFalse(data["ok"])
        self.assertEqual(self.requests, [])

    async def test_status_errors_and_oversized_responses_do_not_fallback(self):
        self.network.http.max_bytes = 1024
        for status, body, expected in [(403, b"private", "json"), (429, b"private", "429"),
                                       (200, b"x" * 2048, "上限"), (200, b"<html>login</html>", "JSON")]:
            with self.subTest(status=status, expected=expected):
                self.handler = lambda request: httpx.Response(status, content=body, headers={"Retry-After": "0"})
                data = await self.call()
                self.assertFalse(data["complete"])
                self.assertIn(expected, data["diagnostics"][0])
                self.assertNotIn("private", data["diagnostics"][0])
                self.assertEqual(len(data["attempts"]), 1)
        self.assertTrue(all(request.url.host == "instance.example" for request in self.requests))

    async def test_redirect_does_not_forward_query_to_another_server(self):
        self.handler = lambda request: httpx.Response(302, headers={"Location": "https://other.example/search?q=private"})
        data = await self.call()
        self.assertFalse(data["ok"])
        self.assertIn("跳转", data["diagnostics"][0])
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("private", json.dumps(data))

    async def test_request_pacing_is_separate_from_builtin(self):
        pacer = self.network.search_pacers["searxng"]
        with patch.object(pacer, "wait", AsyncMock()) as waited:
            await self.call()
        waited.assert_awaited_once()
        self.assertIsNot(pacer, self.network.search_pacers["bing"])

    async def test_batch_deadline_preserves_fast_query_and_cleans_slow_one(self):
        stopped = asyncio.Event()
        async def handler(request):
            if request.url.params["q"] == "slow":
                try:
                    await asyncio.sleep(10)
                finally:
                    stopped.set()
            return response({"results": [row()]})
        self.handler = handler
        data = json.loads(await server.web_search_multi(["fast", "slow"], backend="searxng",
                             response_format="json", time_budget_seconds=0.2))
        self.assertEqual(len(data["results"]), 1)
        self.assertTrue(data["queries"][0]["complete"])
        self.assertTrue(data["queries"][1]["deadline_exceeded"])
        self.assertEqual(data["queries"][1]["backend"], "searxng")
        self.assertEqual(data["queries"][1]["attempts"][0]["status"], "deadline")
        self.assertTrue(stopped.is_set())

    async def test_caller_cancellation_propagates_and_drains_request(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
        self.handler = handler
        task = asyncio.create_task(self.call())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())


class SearXNGMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_search_tools_and_benchmark_over_stdio(self):
        requests = []
        article = (Path(__file__).parent / "fixtures" / "technical_article.html").read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                requests.append(parsed)
                if parsed.path == "/prefix/search":
                    query = parse_qs(parsed.query)["q"][0]
                    result_url = ("https://zhuanlan.zhihu.com/p/1" if "site:zhihu.com" in query else
                                  f"http://127.0.0.1:{self.server.server_port}/article")
                    body = json.dumps({"results": [row(result_url)], "unresponsive_engines": []}).encode()
                    mime = "application/json"
                else:
                    body, mime = article, "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(http.shutdown)
        env = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
               "RESOURCER_SEARXNG_URL": f"http://127.0.0.1:{http.server_port}/prefix",
               "RESOURCER_SEARCH_RPM": "0", "RESOURCER_FETCH_RPM": "0"}
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "server.py")], env=env)
        async def workflow():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = {"web_search", "web_search_multi", "search_chinese", "deep_research"}
                    for tool in listed.tools:
                        if tool.name in names:
                            self.assertEqual(tool.inputSchema["properties"]["backend"]["enum"], ["builtin", "searxng"])
                            self.assertNotIn("ctx", tool.inputSchema["properties"])
                    async def call(name, **args):
                        result = await session.call_tool(name, {"backend": "searxng", "response_format": "json", **args})
                        self.assertFalse(result.isError)
                        return json.loads("\n".join(c.text for c in result.content if c.type == "text"))
                    single = await call("web_search", query="guide")
                    self.assertEqual(single["backend"], "searxng")
                    multi = await call("web_search_multi", queries=["first", "second"])
                    self.assertEqual(multi["results"][0]["queries"], ["first", "second"])
                    self.assertTrue(all(q["backend"] == "searxng" for q in multi["queries"]))
                    chinese = await call("search_chinese", query="异步")
                    self.assertEqual(chinese["results"][0]["url"], "https://zhuanlan.zhihu.com/p/1")
                    research = await call("deep_research", query="guide", fetch_top_n=1, content_format="markdown")
                    self.assertTrue(research["ok"], research)
                    self.assertEqual(research["successful_pages"], 1)
                    self.assertIn("```", research["pages"][0]["text"])
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env):
                case = {"id": "fixture", "language": "en", "query": "guide", "include_domains": ["127.0.0.1"],
                        "reference_urls": [f"http://127.0.0.1:{http.server_port}/article"]}
                output = Path(tmp) / "report.json"
                report = await search_smoke.run([case], ["fallback"], 1, 5, 15, output, "searxng")
                self.assertTrue(report["run_complete"])
                self.assertEqual(report["settings"]["backend"], "searxng")
                self.assertTrue(report["records"][0]["reference_found"])
                self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["records"], report["records"])
        await asyncio.wait_for(workflow(), 40)
        self.assertEqual(sum(r.path == "/prefix/search" for r in requests), 6)
        self.assertEqual(sum(r.path == "/article" for r in requests), 1)
