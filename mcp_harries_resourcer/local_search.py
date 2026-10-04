"""Bounded local text search and filtering of save_finding metadata."""
import asyncio
import json
import math
import os
from pathlib import Path
import stat
import threading
import time

from .search_results import domain_matches, normalize_domains, url_identity
from .local_results import RankedFiles, match_preview
from .local_terms import TermQuery

DEFAULT_EXCLUDES = ".git,node_modules,__pycache__,.venv,venv,.next,dist,build,.godot"
DEFAULT_EXTENSIONS = "txt,md,py,js,ts,json,html,htm,csv,yaml,yml,toml,gd,godot"


async def search_async(*args, **kwargs) -> dict:
    """Keep filesystem work off the MCP loop; wait for the worker on cancellation."""
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(search, *args, **kwargs, cancelled=cancelled))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def _preview_metadata(meta: dict) -> tuple[dict, bool]:
    result = {}
    for key, value in meta.items():
        if key == "tags":
            result[key] = [tag[:64] for tag in value[:32]]
        else:
            result[key] = value[:2048 if key == "source_url" else 256]
    return result, result != meta


def _metadata(lines: list[str]) -> tuple[dict, bool]:
    """Read our JSON-valued frontmatter, not arbitrary YAML or executable tags."""
    if not lines or lines[0] != "---":
        return {}, False
    result = {}
    for line in lines[1:257]:
        if line == "---":
            return result, False
        key, sep, value = line.partition(":")
        if key not in {"title", "collection", "saved_at", "source_url", "tags"}:
            continue
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return {}, True
        valid = (isinstance(parsed, list) and all(isinstance(t, str) for t in parsed)
                 if key == "tags" else isinstance(parsed, str))
        if not sep or not valid or key in result:
            return {}, True
        result[key] = parsed
    return {}, True


def search(query: str, root: str, max_results: int = 50,
           include_ext: str = DEFAULT_EXTENSIONS, *, collection: str = "",
           tags: str = "", source_domain: str = "", exclude_dirs: str = DEFAULT_EXCLUDES,
           max_entries: int = 100000, max_file_bytes: int = 2 * 1024 * 1024,
           time_budget_seconds: float = 10, result_mode: str = "lines", query_mode: str = "literal",
           cancelled: threading.Event | None = None) -> dict:
    started = time.monotonic()
    if result_mode not in {"lines", "files"}:
        raise ValueError("result_mode 必须是 lines 或 files")
    if query_mode not in {"literal", "all", "any"}:
        raise ValueError("query_mode 必须是 literal、all 或 any")
    terms = TermQuery(query, query_mode) if query_mode != "literal" else None
    for name, value, upper in (("max_results", max_results, 1000),
                               ("max_entries", max_entries, 1000000),
                               ("max_file_bytes", max_file_bytes, 16 * 1024 * 1024)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ValueError(f"{name} 必须是 1–{upper} 的整数")
    if not math.isfinite(time_budget_seconds) or not 0 < time_budget_seconds <= 600:
        raise ValueError("time_budget_seconds 必须大于 0 且不超过 600")
    requested_tags = {t.strip().casefold() for t in tags.split(",") if t.strip()}
    wanted_collection = collection.strip().casefold()
    domains = normalize_domains([source_domain]) if source_domain.strip() else []
    filtering = bool(wanted_collection or requested_tags or domains)
    if not query.strip() and not filtering:
        raise ValueError("关键词不能为空；仅浏览笔记时请指定集合、标签或来源筛选")
    needle = query.casefold() if query.strip() else ""
    excluded = {d.strip().casefold() for d in exclude_dirs.split(",") if d.strip()}
    if any(any(c in d for c in "/\\*?[]") or d in {".", ".."} for d in excluded):
        raise ValueError("exclude_dirs 只接受逗号分隔的目录名，不接受路径或通配符")
    extensions = {"." + e.strip().lower().lstrip(".") for e in include_ext.split(",") if e.strip()}
    root_path = Path(root).expanduser().absolute()
    if not root_path.exists():
        raise ValueError(f"目录不存在：{root}")
    if not root_path.is_dir():
        raise ValueError(f"不是目录：{root}")
    skipped = dict.fromkeys(("excluded_dirs", "extensions", "links", "special_files",
                            "oversized", "binary", "encoding", "io_errors", "metadata"), 0)
    stats = {"entries": 0, "files_read": 0, "files_searched": 0, "filtered_files": 0}
    hits, pending = [], [root_path]
    ranked = RankedFiles(max_results) if result_mode == "files" else None
    stop_reason = None

    def expired():
        nonlocal stop_reason
        if cancelled is not None and cancelled.is_set():
            stop_reason = "cancelled"
            return True
        if time.monotonic() - started >= time_budget_seconds:
            stop_reason = "time_budget"
            return True
        return False

    def scan_file(path, size):
        nonlocal stop_reason
        if extensions and path.suffix.lower() not in extensions:
            skipped["extensions"] += 1
            return
        if size > max_file_bytes:
            skipped["oversized"] += 1
            return
        with path.open("rb") as source:
            chunks, total = [], 0
            while total <= max_file_bytes:
                if expired():
                    return
                chunk = source.read(min(65536, max_file_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
        stats["files_read"] += 1
        if len(raw) > max_file_bytes:  # File may have grown since stat.
            skipped["oversized"] += 1
            return
        if b"\0" in raw:
            skipped["binary"] += 1
            return
        try:
            lines = raw.decode("utf-8-sig").splitlines()
        except UnicodeDecodeError:
            skipped["encoding"] += 1
            return
        meta, invalid = _metadata(lines) if path.suffix.lower() == ".md" else ({}, False)
        if invalid:
            skipped["metadata"] += 1
        accepted = not wanted_collection or str(meta.get("collection", "")).strip().casefold() == wanted_collection
        accepted = accepted and requested_tags.issubset({t.casefold() for t in meta.get("tags", [])})
        if domains:
            try:
                _, host = url_identity(meta.get("source_url", ""))
                accepted = accepted and domain_matches(host, domains[0])
            except ValueError:
                accepted = False
        if not accepted:
            stats["filtered_files"] += 1
            return
        stats["files_searched"] += 1
        preview, preview_truncated = _preview_metadata(meta)
        base = {"path": str(path), "metadata": preview, "metadata_truncated": preview_truncated}
        if terms is not None and needle:
            if ranked is not None:
                match = terms.file(lines, meta, path.name, base, expired)
                if match is not None:
                    hit, priority = match
                    ranked.add(hit, priority, path.relative_to(root_path).as_posix())
            else:
                for hit in terms.lines(lines, base, expired):
                    hits.append(hit)
                    if len(hits) >= max_results:
                        stop_reason = "max_results"
                        break
            return
        fields = []
        first_hit = None
        matched_lines = 0
        if ranked is not None and needle:
            field_values = {"title": meta.get("title", ""), "filename": path.name,
                            "tags": next((tag for tag in meta.get("tags", []) if needle in tag.casefold()), "")}
            fields = [name for name in ("title", "tags", "filename") if needle in field_values[name].casefold()]
        for lineno, line in enumerate(lines if needle else [meta.get("title", path.name)], 1):
            if expired():
                return
            folded = line.casefold()
            position = folded.find(needle) if needle else 0
            if position >= 0:
                matched_lines += bool(needle)
                if ranked is not None and first_hit is not None:
                    continue
                window = match_preview(line, folded, position, needle, expired)
                if window is None:
                    return
                hit = {**base, "line": lineno if needle else None, **window}
                if not needle:
                    hit["column"] = None
                if ranked is not None:
                    first_hit = hit
                    continue
                hits.append(hit)
                if len(hits) >= max_results:
                    stop_reason = "max_results"
                    return
        if ranked is not None:
            if matched_lines:
                fields.append("text")
            if needle and not fields:
                return
            if first_hit is None:
                field = fields[0]
                value = field_values[field]
                folded = value.casefold()
                window = match_preview(value, folded, folded.find(needle), needle, expired)
                if window is None:
                    return
                first_hit = {**base, "line": None, **window, "preview_source": field}
            else:
                first_hit["preview_source"] = "line" if needle else "metadata"
            first_hit.update(matched_fields=fields, matched_lines=matched_lines)
            priority = {"title": 4, "tags": 3, "filename": 2, "text": 1}.get(fields[0] if fields else "", 0)
            ranked.add(first_hit, priority, path.relative_to(root_path).as_posix())

    while pending and not stop_reason:
        if expired():
            break
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if expired():
                        break
                    if stats["entries"] >= max_entries:
                        stop_reason = "max_entries"
                        break
                    stats["entries"] += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if (stat.S_ISLNK(info.st_mode) or
                                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                            skipped["links"] += 1
                        elif stat.S_ISDIR(info.st_mode):
                            if entry.name.casefold() in excluded:
                                skipped["excluded_dirs"] += 1
                            else:
                                pending.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode):
                            scan_file(Path(entry.path), info.st_size)
                        else:
                            skipped["special_files"] += 1
                    except OSError:
                        skipped["io_errors"] += 1
                    if stop_reason:
                        break
        except OSError:
            skipped["io_errors"] += 1
    incomplete = bool(stop_reason or any(skipped[k] for k in ("oversized", "binary", "encoding", "io_errors"))
                      or (filtering and skipped["metadata"]))
    selection = {}
    if ranked is not None:
        hits = ranked.results()
        selection = {"scan_complete": not incomplete, "matched_files": ranked.count,
                     "results_truncated": ranked.count > len(hits)}
        incomplete = incomplete or selection["results_truncated"]
    return {"ok": True, "query": query, "root": str(root_path), "matches": hits, "result_mode": result_mode,
            "query_mode": query_mode, **({"query_terms": list(terms.terms)} if terms is not None else {}),
            "complete": not incomplete, "stop_reason": stop_reason,
            "filters": {"collection": collection, "tags": sorted(requested_tags), "source_domain": domains[0] if domains else ""},
            "stats": stats, "skipped": skipped, "elapsed_seconds": round(time.monotonic() - started, 4), **selection}


def format_result(result: dict) -> str:
    hits = result["matches"]
    files = result.get("result_mode") == "files"
    has_query = bool(result["query"].strip())
    order = "标题、标签、文件名、全文依次优先" if has_query else "按相对路径排序"
    if has_query and result.get("query_mode") == "all":
        order = "按全部关键词可覆盖的字段层级排序"
    lines = [f"# 本地搜索：{result['query'] if has_query else '按元数据筛选'}", f"root: {result['root']}",
             f"返回 {len(hits)} 个文件（{order}）" if files else f"命中 {len(hits)} 处"]
    if has_query and result.get("query_mode") in {"all", "any"}:
        relation = "全部" if result["query_mode"] == "all" else "任一"
        scope = "同一文件" if files else "同一行"
        lines.append(f"匹配规则：{scope}命中{relation}关键词；空白分词、标点按字面匹配。")
    if files:
        if not result["scan_complete"]:
            lines.append(f"扫描不完整（{result['stop_reason'] or '有文件未能搜索'}）；排序仅限已完成扫描的文件。")
        if result["results_truncated"]:
            lines.append(f"已检查的文件中有 {result['matched_files']} 个命中，仅展示排名前 {len(hits)} 个。")
        if result["complete"] and not hits:
            lines.append("在本次搜索范围内未找到匹配。")
    elif not result["complete"]:
        lines.append(f"搜索不完整（{result['stop_reason'] or '有文件未能搜索'}）；未命中不代表目录中没有相关资料。")
    elif not hits:
        lines.append("在本次搜索范围内未找到匹配。")
    current = None
    for hit in hits:
        if hit["path"] != current:
            current = hit["path"]
            lines.append(f"\n**{current}**")
        for preview in hit.get("term_matches", [hit]):
            prefix = f"L{preview['line']}: " if preview["line"] is not None else ""
            term = f"[{preview['term']}] " if "term" in preview else ""
            source = f"{preview['preview_source']}: " if "term" in preview and preview["line"] is None else ""
            before = "…" if preview.get("text_start", 0) else ""
            after = "…" if preview.get("text_end", 0) < preview.get("source_chars", 0) else ""
            lines.append(f"  {term}{source}{prefix}{before}{preview['text']}{after}")
        if files and hit["matched_fields"]:
            lines.append("  命中字段：" + ", ".join(hit["matched_fields"]) + f"；全文命中 {hit['matched_lines']} 行")
            if "rank_field" in hit:
                lines.append("  排序层级：" + hit["rank_field"])
    lines.append("\n扫描：" + json.dumps(result["stats"], ensure_ascii=False))
    lines.append("跳过：" + json.dumps({k: v for k, v in result["skipped"].items() if v}, ensure_ascii=False))
    return "\n".join(lines)
