"""Compare line and file retrieval on generated notes, without private data.

The topic note has an explicit matching title; other notes only mention the
query in their content. This demonstrates field priority, not general relevance.
Set TEMP/TMP to the intended scratch disk before running.
"""
import argparse
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_harries_resourcer import __version__
from mcp_harries_resourcer.local_search import search


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    records = []
    with tempfile.TemporaryDirectory(prefix="resourcer-ranking-") as tmp:
        root = Path(tmp)
        for index in range(999):
            (root / f"{index:04d}-log.md").write_text(
                "incidental asyncio mention; 取消 background discussion\n" * 20, encoding="utf-8")
        target = root / "zzz-guide.md"
        target.write_text('---\ntitle: "asyncio 取消指南"\ntags: ["Python"]\n---\n专题资料。', encoding="utf-8")
        for query in ("asyncio", "取消"):
            for repeat in range(3):
                for mode in (("lines", "files") if repeat % 2 == 0 else ("files", "lines")):
                    tracemalloc.start()
                    started = time.perf_counter()
                    try:
                        result = search(query, tmp, max_results=5, result_mode=mode, time_budget_seconds=60)
                        elapsed = time.perf_counter() - started
                        _, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    paths = [Path(hit["path"]).name for hit in result["matches"]]
                    if mode == "files":
                        assert result["scan_complete"] and paths[0] == target.name, result
                        assert result["matched_files"] == 1000 and len(set(paths)) == 5, result
                    records.append({"query": query, "mode": mode, "run": repeat + 1,
                                    "elapsed_seconds": elapsed, "python_peak_bytes": peak,
                                    "paths": paths, "topic_note_returned": target.name in paths,
                                    "unique_files_returned": len(set(paths)), "stats": result["stats"],
                                    "scan_complete": result.get("scan_complete", result["complete"]),
                                    "complete": result["complete"], "stop_reason": result["stop_reason"]})
        report = {"version": __version__, "python": platform.python_version(), "os": platform.system(),
                  "scratch_drive": root.anchor, "synthetic": True, "files": 1000, "max_results": 5,
                  "records": records}
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
