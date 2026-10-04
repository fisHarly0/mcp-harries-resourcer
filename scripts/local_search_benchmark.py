"""Reproducible generated-tree benchmark; no private notes or network access.

Set TEMP/TMP to the desired scratch disk before running. The baseline reproduces
the previous rglob traversal and whole-file reads, not a competing search engine.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_harries_resourcer.local_search import DEFAULT_EXCLUDES, search


def baseline(root):
    hits = []
    excluded = set(DEFAULT_EXCLUDES.split(","))
    for path in root.rglob("*"):
        if any(part in excluded for part in path.parts):
            continue
        if not path.is_file() or path.suffix != ".md":
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(content.splitlines(), 1):
            if "unique needle" in line.lower():
                hits.append((str(path), number))
    return hits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="resourcer-search-") as tmp:
        root = Path(tmp)
        notes = root / "notes"
        notes.mkdir()
        for i in range(1000):
            (notes / f"{i}.md").write_text("ordinary text\n" * 20 + ("unique needle" if i == 999 else "end"), encoding="utf-8")
        for i in range(100):
            ignored = root / "node_modules" / f"package-{i}"
            ignored.mkdir(parents=True)
            for j in range(100):
                (ignored / f"{j}.md").write_text("unique needle", encoding="utf-8")
        timings = {"baseline": [], "current": []}
        for trial in range(4):
            # Alternate order to reduce filesystem-cache ordering bias.
            for name in (("baseline", "current") if trial % 2 == 0 else ("current", "baseline")):
                started = time.perf_counter()
                if name == "baseline":
                    old = baseline(root)
                else:
                    current = search("unique needle", str(root), include_ext="md", time_budget_seconds=60)
                timings[name].append(time.perf_counter() - started)
        assert current["complete"]
        assert old == [(h["path"], h["line"]) for h in current["matches"]]
        report = {"python": sys.version, "platform": sys.platform,
                  "scratch_drive": Path(tmp).anchor, "notes": 1000, "excluded_files": 10000,
                  "timings_seconds": timings,
                  "median_seconds": {k: statistics.median(v) for k, v in timings.items()},
                  "same_matches": True, "stats": current["stats"], "skipped": current["skipped"]}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
