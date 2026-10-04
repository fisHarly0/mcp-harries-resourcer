"""Offline HTTP fixtures in a real MCP stdio server subprocess."""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import server


async def respond(request):
    if request.url.host == "cn.bing.com" and "budget" in request.url.params.get("q", ""):
        await asyncio.Event().wait()
    if request.url.host == "html.duckduckgo.com":
        return httpx.Response(200, text='''<div class="result"><a class="result__a"
            href="https://docs.python.org/guide#ddg">Python guide</a></div>
            <div class="result"><a class="result__a"
            href="https://python.org.evil.org/guide">Unwanted</a></div>''')
    if request.url.host == "cn.bing.com":
        return httpx.Response(200, text='''<ol id="b_results"><li class="b_algo"><h2>
            <a href="https://docs.python.org:443/guide#bing">Python guide</a>
            </h2></li></ol>''')
    raise AssertionError(f"Unexpected request host: {request.url.host}")


RealClient = httpx.AsyncClient
server.httpx.AsyncClient = lambda **kwargs: RealClient(transport=httpx.MockTransport(respond), trust_env=False, **kwargs)
server.NETWORK = server.RequestPolicy(search_rpm=0, fetch_rpm=0)
server.mcp.run(transport="stdio")
