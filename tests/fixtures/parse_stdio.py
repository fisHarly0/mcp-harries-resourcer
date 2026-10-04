"""Actual MCP and parser processes with offline HTTP responses."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
from mcp_harries_resourcer import server

RealClient = httpx.AsyncClient
def respond(request):
    if os.environ.get("TEST_SEARCH_PARSE"):
        engine = "ddg" if "duckduckgo" in request.url.host else "bing"
        return httpx.Response(200, text=Path(__file__).with_name(f"{engine}_results.html").read_text(encoding="utf-8"))
    return httpx.Response(200, text="Readable body")

server.httpx.AsyncClient = lambda **kw: RealClient(
    transport=httpx.MockTransport(respond), trust_env=False, **kw)
server.NETWORK = server.RequestPolicy(search_rpm=0, fetch_rpm=0)
server.PARSER = server.ParsePolicy(command=[sys.executable,
    str(Path(__file__).with_name("parse_process.py")),
    "search_gate" if os.environ.get("TEST_SEARCH_PARSE") else "gate", os.environ["TEST_PARSE_MARKER"]])

@server.mcp.tool()
def parser_state() -> str:
    return json.dumps({"active": len(server.PARSER.active), "inflight": len(server.NETWORK.inflight),
                       "cached": len(server.NETWORK.cache.entries)})

server.mcp.run(transport="stdio")
