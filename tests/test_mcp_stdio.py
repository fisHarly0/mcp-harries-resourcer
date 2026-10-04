"""Exercise the actual MCP wire protocol without external network access."""
import asyncio
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_recovery_and_limits_over_mcp(self):
        counts = {}
        stop = threading.Event()
        article = ("<html><title>Network fixture</title><article><h1>Network fixture</h1><p>" +
                   "A useful article with enough text to extract and verify network recovery. " * 12 +
                   "</p></article></html>").encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                counts[self.path] = counts.get(self.path, 0) + 1
                if self.path == "/retry" and counts[self.path] == 1:
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                if self.path == "/cooldown":
                    self.send_response(429)
                    self.send_header("Retry-After", "60")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if self.path == "/large":
                    self.send_header("Content-Length", "1000000")
                if self.path == "/gzip":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                try:
                    if self.path == "/slow":
                        while not stop.wait(0.1):
                            self.wfile.write(b"slow but still sending ")
                            self.wfile.flush()
                    elif self.path == "/gzip":
                        self.wfile.write(gzip.compress(b"x" * 100000))
                    else:
                        self.wfile.write(article)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.addCleanup(stop.set)
        base = f"http://127.0.0.1:{http.server_port}"
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "server.py")],
            env={**os.environ, "RESOURCER_FETCH_RPM": "0", "RESOURCER_HTTP_RETRIES": "1",
                 "RESOURCER_REQUEST_TIMEOUT_SECONDS": "3", "RESOURCER_RESPONSE_MAX_BYTES": "2048"})

        async def workflow():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    async def fetch(path):
                        result = await session.call_tool("fetch_page", {"url": base + path, "response_format": "json"})
                        self.assertFalse(result.isError)
                        return json.loads("\n".join(c.text for c in result.content if c.type == "text"))
                    recovered = await fetch("/retry")
                    self.assertTrue(recovered["ok"], recovered)
                    self.assertEqual(recovered["request_info"]["retries"], 1)
                    self.assertEqual(counts["/retry"], 2)
                    for path, code in [("/large", "response_too_large"), ("/gzip", "response_too_large"),
                                       ("/slow", "deadline")]:
                        data = await fetch(path)
                        self.assertFalse(data["ok"])
                        self.assertEqual(data["error_code"], code)
                        self.assertEqual(data["text"], "")
                    limited = await fetch("/cooldown")
                    self.assertEqual(limited["error"], "HTTP 429")
                    blocked = await fetch("/after-cooldown")
                    self.assertEqual(blocked["error_code"], "cooldown")
                    self.assertNotIn("/after-cooldown", counts)
        await asyncio.wait_for(workflow(), 30)

    async def test_search_controls_over_mcp(self):
        params = StdioServerParameters(command=sys.executable, args=[
            str(Path(__file__).parent / "fixtures" / "search_stdio.py")], env=dict(os.environ))

        async def workflow():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    for name in ["web_search", "web_search_multi", "deep_research"]:
                        schema = next(t.inputSchema for t in listed.tools if t.name == name)
                        self.assertIn("include_domains", schema["properties"])
                        self.assertEqual(schema["properties"]["strategy"]["enum"], ["fallback", "merge"])

                    async def call(name, **options):
                        result = await session.call_tool(name, {"response_format": "json", **options})
                        self.assertFalse(result.isError)
                        return json.loads("\n".join(c.text for c in result.content if c.type == "text"))

                    result = await call("web_search", query="python", strategy="merge", include_domains=["python.org"])
                    self.assertEqual(len(result["results"]), 1)
                    self.assertEqual(len(result["results"][0]["provenance"]), 2)
                    self.assertEqual(result["filtered_count"], 1)
                    batch = await call("web_search_multi", queries=["python", "异步"], strategy="merge", include_domains=["python.org"])
                    self.assertEqual(len(batch["results"]), 1)
                    self.assertEqual(batch["results"][0]["queries"], ["python", "异步"])
                    invalid = await call("web_search", query="python", include_domains=["https://python.org"])
                    self.assertFalse(invalid["ok"])
                    chinese = await call("search_chinese", query="异步", site="zhihu")
                    self.assertEqual(chinese["search_status"], "filtered_empty")
                    research = await call("deep_research", query="python", include_domains=["python.org"], fetch_top_n=0)
                    self.assertEqual(research["search_status"], "results")
                    self.assertEqual(research["pages"], [])
        await asyncio.wait_for(workflow(), timeout=30)

    async def test_paginated_json_and_refresh_over_mcp(self):
        state = {"version": "alpha", "requests": 0}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                state["requests"] += 1
                body = ("<html><head><title>Local reading test</title></head><body><article>"
                        "<h1>Local reading test</h1><p>" +
                        (f"{state['version']} 中文资料测试，分段读取应保留完整正文与版本信息。 " * 12) +
                        "</p></article></body></html>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        url = f"http://127.0.0.1:{http.server_port}/article"
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "server.py")],
            env={**os.environ, "RESOURCER_FETCH_RPM": "0", "RESOURCER_CACHE_TTL_SECONDS": "300",
                 "RESOURCER_CACHE_MAX_ENTRIES": "64", "RESOURCER_CACHE_MAX_BYTES": "8388608"},
        )
        async def workflow():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    schema = next(t.inputSchema for t in listed.tools if t.name == "fetch_page")
                    self.assertIn("expected_content_id", schema["properties"])
                    self.assertEqual(schema["properties"]["response_format"]["enum"], ["text", "json"])
                    async def fetch(**options):
                        result = await session.call_tool("fetch_page", {"url": url, "response_format": "json", **options})
                        self.assertFalse(result.isError)
                        return json.loads("\n".join(c.text for c in result.content if c.type == "text"))
                    first = await fetch(max_chars=20)
                    rest = await fetch(start_index=first["next_index"], max_chars=0, expected_content_id=first["content_id"])
                    full = await fetch(max_chars=0)
                    self.assertEqual(first["text"] + rest["text"], full["text"])
                    self.assertTrue(rest["cached"])
                    self.assertEqual(state["requests"], 1)
                    state["version"] = "omega"
                    changed = await fetch(refresh=True, expected_content_id=first["content_id"])
                    self.assertFalse(changed["ok"])
                    self.assertIn("版本已变化", changed["error"])
                    latest = await fetch()
                    self.assertTrue(latest["ok"])
                    self.assertIn("omega", latest["text"])
                    self.assertEqual(state["requests"], 2)
        await asyncio.wait_for(workflow(), timeout=30)

    async def test_initialize_list_save_and_search(self):
        async def workflow():
            with tempfile.TemporaryDirectory() as tmp:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(Path(__file__).resolve().parents[1] / "server.py")],
                    env={**os.environ, "RESOURCER_RESEARCH_ROOT": tmp},
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        self.assertEqual({t.name for t in listed.tools}, {
                            "web_search", "web_search_multi", "search_chinese", "fetch_page",
                            "fetch_pages", "deep_research", "search_local", "save_finding",
                        })
                        saved = await session.call_tool("save_finding", {
                            "collection": "smoke", "title": "MCP note", "content": "Protocol workflow verified.",
                        })
                        self.assertFalse(saved.isError)
                        self.assertEqual(len(list((Path(tmp) / "smoke").glob("*.md"))), 1)
                        found = await session.call_tool("search_local", {"query": "Protocol workflow verified", "root": tmp})
                        self.assertFalse(found.isError)
                        self.assertIn("Protocol workflow verified", "\n".join(c.text for c in found.content if c.type == "text"))
                        empty = await session.call_tool("web_search", {"query": " "})
                        self.assertIn("关键词不能为空", "\n".join(c.text for c in empty.content if c.type == "text"))
        await asyncio.wait_for(workflow(), timeout=30)
