"""Exercise the actual MCP wire protocol without external network access."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
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
