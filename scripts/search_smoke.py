"""Live search evidence: domain correctness and known-reference retrieval separately.

Uses real MCP and makes engine requests. Does not infer semantic relevance from
matching domains, nor treat blocked engines as proof that a document is absent.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def url_key(url):
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        raise ValueError("URL contains invalid characters")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        raise ValueError("URL must be HTTP(S) without user info")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port, parsed.path.rstrip("/") or "/"


def matches_domain(host, domains):
    # Independent of the server's filter implementation.
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def load_cases(path):
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a non-empty JSON array")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or any(not isinstance(case.get(k), str) or not case[k].strip()
                                             for k in ("id", "language", "query")):
            raise ValueError("every case needs non-empty id, language and query")
        if case["id"] in seen:
            raise ValueError("case ids must be unique")
        seen.add(case["id"])
        domains, references = case.get("include_domains"), case.get("reference_urls")
        if not isinstance(domains, list) or not domains or not all(isinstance(d, str) and d for d in domains):
            raise ValueError("include_domains must be a non-empty list")
        if any(len(d) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                   for label in d.split(".")) for d in domains):
            raise ValueError("include_domains must use lowercase bare hostnames")
        if not isinstance(references, list) or not references or not all(isinstance(u, str) for u in references):
            raise ValueError("reference_urls must be a non-empty list")
        if any(not matches_domain(url_key(u)[1], domains) for u in references):
            raise ValueError("reference URLs must satisfy the case's domains")
    return cases


def evaluate(case, results):
    references = {url_key(url) for url in case["reference_urls"]}
    checked, violations, reference_ranks = [], [], []
    for rank, item in enumerate(results, 1):
        url = item.get("url", "")
        try:
            key = url_key(url)
            allowed = matches_domain(key[1], case["include_domains"])
            reference = allowed and key in references
        except (TypeError, ValueError):
            allowed, reference = False, False
        if not allowed:
            violations.append(url)
        if reference:
            reference_ranks.append(rank)
        checked.append({"rank": rank, "url": url, "domain_allowed": allowed, "reference_match": reference})
    first = reference_ranks[0] if reference_ranks else None
    return {"checked_links": len(results), "domain_check": not violations if results else None,
            "domain_violations": violations, "reference_found": bool(first), "first_reference_rank": first,
            "reciprocal_rank": 1 / first if first else 0, "checked_results": checked}


def summarize(records):
    groups = {}
    for record in records:
        key = (record["strategy"], record["language"])
        groups.setdefault(key, []).append(record)
    return [{"strategy": strategy, "language": language, "runs": len(rows),
             "with_results": sum(bool(r["checked_links"]) for r in rows),
             "reference_found": sum(r["reference_found"] for r in rows),
             "mean_reciprocal_rank": round(statistics.mean(r["reciprocal_rank"] for r in rows), 4),
             "complete_searches": sum(r.get("complete") is True for r in rows),
             "incomplete_without_reference": sum(not r.get("complete") and not r["reference_found"] for r in rows),
             "domain_violations": sum(len(r["domain_violations"]) for r in rows),
             "median_elapsed_seconds": round(statistics.median(r["elapsed_seconds"] for r in rows), 3)}
            for (strategy, language), rows in sorted(groups.items())]


def checkpoint(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    report["summary"] = summarize(report["records"])
    report["checked_at"] = datetime.now(timezone.utc).isoformat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


async def run(cases, strategies, repeats, per_query, budget, output):
    repo = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted((repo / "mcp_harries_resourcer").glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, stderr=subprocess.DEVNULL, text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    report = {"schema_version": 2, "run_complete": False, "repo_commit": commit,
              "worktree_dirty": dirty, "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "source_sha256": digest.hexdigest(), "cases": cases,
              "planned_runs": len(cases) * len(strategies) * repeats,
              "settings": {"strategies": strategies, "repeats": repeats, "per_query": per_query,
                           "time_budget_seconds": budget, "tool": "web_search_multi", "queries_per_call": 1},
              "records": []}
    checkpoint(output, report)
    params = StdioServerParameters(command=sys.executable, args=[str(repo / "server.py")], env=dict(os.environ))
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for repeat in range(repeats):
                    for index, case in enumerate(cases):
                        # Alternate order to reduce systematic second-strategy throttling bias.
                        order = strategies if (index + repeat) % 2 == 0 else list(reversed(strategies))
                        for strategy in order:
                            started = time.monotonic()
                            result = await session.call_tool("web_search_multi", {
                                "queries": [case["query"]], "per_query": per_query, "strategy": strategy,
                                "include_domains": case["include_domains"], "response_format": "json",
                                "time_budget_seconds": budget})
                            if result.isError:
                                raise RuntimeError("MCP tool returned an error; inspect server stderr")
                            payload = json.loads("\n".join(c.text for c in result.content if c.type == "text"))
                            state = payload["queries"][0]
                            record = {**state, "case_id": case["id"], "language": case["language"],
                                      "strategy": strategy, "repeat": repeat + 1,
                                      "elapsed_seconds": round(time.monotonic() - started, 3),
                                      "results": payload["results"], "batch": payload["batch"],
                                      **evaluate(case, payload["results"])}
                            report["records"].append(record)
                            checkpoint(output, report)
                            print(f"{case['id']} / {strategy}: {record['search_status']}, "
                                  f"{record['checked_links']} links, reference rank={record['first_reference_rank']}", flush=True)
        report["run_complete"] = True
    except BaseException as exc:
        report["run_error"] = type(exc).__name__
        raise
    finally:
        checkpoint(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("search_cases.json"))
    parser.add_argument("--case", action="append", dest="selected", help="Case id; repeat to select several")
    parser.add_argument("--strategies", nargs="+", choices=["fallback", "merge"], default=["fallback", "merge"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--per-query", type=int, default=5)
    parser.add_argument("--time-budget-seconds", type=float, default=30)
    parser.add_argument("--require-reference", action="store_true", help="Exit nonzero if any run misses all reference URLs")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.per_query <= 30:
        parser.error("repeats must be 1–10 and per-query must be 1–30")
    if not math.isfinite(args.time_budget_seconds) or not 0 < args.time_budget_seconds <= 600:
        parser.error("time budget must be positive and at most 600 seconds")
    try:
        cases = load_cases(args.cases)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.selected:
        if set(args.selected) - {case["id"] for case in cases}:
            parser.error("unknown case id")
        cases = [case for case in cases if case["id"] in args.selected]
    report = asyncio.run(run(cases, list(dict.fromkeys(args.strategies)), args.repeats,
                             args.per_query, args.time_budget_seconds, args.output))
    if any(r["domain_check"] is False for r in report["records"]):
        raise SystemExit("Domain invariant failed; inspect JSON evidence")
    if args.require_reference and any(not r["reference_found"] for r in report["records"]):
        raise SystemExit("At least one run missed reference URLs; inspect completion and diagnostics")


if __name__ == "__main__":
    main()
