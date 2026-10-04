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
           time_budget_seconds: float = 10, cancelled: threading.Event | None = None) -> dict:
    started = time.monotonic()
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
        for lineno, line in enumerate(lines if needle else [meta.get("title", path.name)], 1):
            if expired():
                return
            if not needle or needle in line.casefold():
                hits.append({"path": str(path), "line": lineno if needle else None,
                             "text": line.strip()[:200], "metadata": preview,
                             "metadata_truncated": preview_truncated})
                if len(hits) >= max_results:
                    stop_reason = "max_results"
                    return

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
    return {"ok": True, "query": query, "root": str(root_path), "matches": hits,
            "complete": not incomplete, "stop_reason": stop_reason,
            "filters": {"collection": collection, "tags": sorted(requested_tags), "source_domain": domains[0] if domains else ""},
            "stats": stats, "skipped": skipped, "elapsed_seconds": round(time.monotonic() - started, 4)}


def format_result(result: dict) -> str:
    hits = result["matches"]
    lines = [f"# 本地搜索：{result['query'] or '按元数据筛选'}", f"root: {result['root']}", f"命中 {len(hits)} 处"]
    if not result["complete"]:
        lines.append(f"搜索不完整（{result['stop_reason'] or '有文件未能搜索'}）；未命中不代表目录中没有相关资料。")
    elif not hits:
        lines.append("在本次搜索范围内未找到匹配。")
    current = None
    for hit in hits:
        if hit["path"] != current:
            current = hit["path"]
            lines.append(f"\n**{current}**")
        prefix = f"L{hit['line']}: " if hit["line"] is not None else ""
        lines.append(f"  {prefix}{hit['text']}")
    lines.append("\n扫描：" + json.dumps(result["stats"], ensure_ascii=False))
    lines.append("跳过：" + json.dumps({k: v for k, v in result["skipped"].items() if v}, ensure_ascii=False))
    return "\n".join(lines)
