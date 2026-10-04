"""Actual MCP and parser processes with offline HTTP responses."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
from mcp_harries_resourcer import server

RealClient = httpx.AsyncClient
server.httpx.AsyncClient = lambda **kw: RealClient(
    transport=httpx.MockTransport(lambda r: httpx.Response(200, text="Readable body")), trust_env=False, **kw)
server.NETWORK = server.RequestPolicy(fetch_rpm=0)
server.PARSER = server.ParsePolicy(command=[sys.executable,
    str(Path(__file__).with_name("parse_process.py")), "gate", os.environ["TEST_PARSE_MARKER"]])

@server.mcp.tool()
def parser_state() -> str:
    return json.dumps({"active": len(server.PARSER.active), "inflight": len(server.NETWORK.inflight),
                       "cached": len(server.NETWORK.cache.entries)})

server.mcp.run(transport="stdio")
