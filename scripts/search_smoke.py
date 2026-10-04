"""Optional live MCP search comparison; emits evidence, not a quality guarantee.

Run: python scripts/search_smoke.py --output /path/to/search-smoke.json
Uses the active interpreter and existing environment. Makes real engine requests.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run():
    records = []
    params = StdioServerParameters(command=sys.executable,
                                  args=[str(Path(__file__).resolve().parents[1] / "server.py")],
                                  env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for query in ["Python asyncio official documentation", "Python asyncio 官方文档"]:
                for strategy in ["fallback", "merge"]:
                    started = time.monotonic()
                    result = await session.call_tool("web_search", {
                        "query": query, "max_results": 5, "strategy": strategy,
                        "include_domains": ["docs.python.org"], "response_format": "json",
                    })
                    payload = json.loads("\n".join(c.text for c in result.content if c.type == "text"))
                    links = [r["url"] for r in payload.get("results", [])]
                    passed = all(urlsplit(u).hostname == "docs.python.org" or
                                 (urlsplit(u).hostname or "").endswith(".docs.python.org") for u in links)
                    record = {"elapsed_seconds": round(time.monotonic() - started, 2),
                              "checked_links": len(links), "domain_check": passed if links else None,
                              **payload}
                    records.append(record)
                    print(f"{query} / {strategy}: {payload.get('search_status', 'error')}, {len(links)} links", flush=True)
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "records": records}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = asyncio.run(run())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(r["domain_check"] is False for r in evidence["records"]):
        raise SystemExit("Domain invariant failed; inspect the JSON evidence.")
