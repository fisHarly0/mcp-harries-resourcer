"""Offline synthetic search parsing and event-loop latency; not search quality.

Run from a checkout with runtime dependencies installed. The direct mode
intentionally blocks the event loop to compare the same parser without isolation.
"""
import argparse
import asyncio
import gc
import json
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_harries_resourcer import __version__
from mcp_harries_resourcer.parse_policy import ParsePolicy
from mcp_harries_resourcer.search_content import parse_search


async def measure(html, mode):
    gc.collect()
    gaps = []
    async def heartbeat():
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    pulse = asyncio.create_task(heartbeat())
    policy = ParsePolicy()
    try:
        await asyncio.sleep(0.03)
        start = time.perf_counter()
        if mode == "subprocess":
            result = await policy.parse_search(html, "bing", 10)
            assert result["ok"], result
            items = result["items"]
        else:
            items = parse_search(html, "bing", 10)
        elapsed = time.perf_counter() - start
        await asyncio.sleep(0.03)
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
    assert len(items) == 10 and items[0]["url"] == "https://example.org/0", items
    assert not policy.active
    return {"elapsed_seconds": elapsed, "max_heartbeat_gap_seconds": max(gaps),
            "results": len(items), "active_children_after": len(policy.active)}


async def benchmark(repeats):
    records = []
    for count in (10, 5000):
        html = '<ol id="b_results">' + ''.join(
            f'<li class="b_algo"><h2><a href="https://example.org/{i}">Result {i}</a></h2>'
            '<p>Snippet with <b>highlight</b> text.</p></li>' for i in range(count)) + '</ol>'
        for mode in ("direct", "subprocess"):
            for run in range(1, repeats + 1):
                metrics = await measure(html, mode)
                records.append({"rows": count, "bytes": len(html.encode("utf-8")),
                                "mode": mode, "run": run, **metrics})
    return {"version": __version__, "python": platform.python_version(), "os": platform.system(),
            "synthetic": True, "heartbeat_interval_seconds": 0.02, "records": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("--repeats must be between 1 and 100")
    result = json.dumps(asyncio.run(benchmark(args.repeats)), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result + "\n", encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
