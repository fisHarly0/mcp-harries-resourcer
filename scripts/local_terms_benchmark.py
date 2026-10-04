"""Compare literal/AND/OR retrieval on 1,000 generated notes, without private data.

Only the topic note contains both terms. The other notes contain one term each.
This tests explicit matching rules and scan cost, not real-world relevance.
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
    with tempfile.TemporaryDirectory(prefix="resourcer-terms-") as tmp:
        root = Path(tmp)
        for index in range(999):
            term = "asyncio" if index % 2 == 0 else "取消"
            (root / f"{index:04d}-note.md").write_text((term + " background discussion\n") * 20, encoding="utf-8")
        target = root / "zzz-guide.md"
        target.write_text('---\ntitle: "asyncio 指南"\n---\n任务取消与超时处理。', encoding="utf-8")
        for query in ("asyncio 取消", "取消 asyncio"):
            for repeat in range(3):
                for mode in (("literal", "all", "any") if repeat % 2 == 0 else ("any", "all", "literal")):
                    tracemalloc.start()
                    started = time.perf_counter()
                    try:
                        result = search(query, tmp, result_mode="files", query_mode=mode,
                                        max_results=5, time_budget_seconds=60)
                        elapsed = time.perf_counter() - started
                        _, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    paths = [Path(hit["path"]).name for hit in result["matches"]]
                    assert result["scan_complete"], result
                    assert result["matched_files"] == {"literal": 0, "all": 1, "any": 1000}[mode], result
                    if mode == "all":
                        assert paths == [target.name] and result["complete"], result
                        assert all(e["term"].casefold() in e["text"].casefold() for e in result["matches"][0]["term_matches"]), result
                    records.append({"query": query, "query_mode": mode, "run": repeat + 1,
                                    "elapsed_seconds": elapsed, "python_peak_bytes": peak,
                                    "paths": paths, "topic_note_returned": target.name in paths,
                                    "matched_files": result["matched_files"], "stats": result["stats"],
                                    "scan_complete": result["scan_complete"], "complete": result["complete"],
                                    "results_truncated": result["results_truncated"]})
        report = {"version": __version__, "python": platform.python_version(), "os": platform.system(),
                  "scratch_drive": root.anchor, "synthetic": True, "files": 1000, "max_results": 5,
                  "records": records}
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
