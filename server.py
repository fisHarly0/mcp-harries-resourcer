"""MCP server: 资料查找助手 (resourcer)

工具：
  - web_search        : 中文优先 Bing，其他优先 DDG，失败切换并附诊断
  - web_search_multi  : 并发跑多个 query，结果合并去重
  - fetch_page        : 抓取单页正文
  - fetch_pages       : 并发批量抓取
  - deep_research     : 搜索 + 抓前 N 个正文，返回资料包供调用方总结
  - search_local      : 本地目录全文搜索
  - search_chinese    : 知乎/B站/微信公众号 定向搜索
  - save_finding      : 把整理好的资料写入 research collection 目录

通过 stdio 与 MCP 客户端通信，所以禁止 print 到 stdout。
"""

import asyncio
import base64
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus, parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import Context, FastMCP

from request_policy import RequestPolicy
from http_policy import HTTPPolicyError
from batch_budget import BatchBudget, Unfinished
from page_content import content_warnings
from parse_policy import ParseFailure, ParsePolicy
from task_cleanup import cancel_and_wait, wait_for_owned
from progress import CompletionCounter, report, with_progress
from client_pool import ClientPool
from local_search import DEFAULT_EXCLUDES, DEFAULT_EXTENSIONS, search_async as local_search, format_result as format_local
from search_results import merge_results, normalize_domains, select_results, url_identity

DEFAULT_TIMEOUT = 25.0
FETCH_CONCURRENCY = 5
HTTP_CLIENTS = ClientPool(lambda: httpx.AsyncClient(
    timeout=DEFAULT_TIMEOUT, follow_redirects=True,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30)))


@asynccontextmanager
async def server_lifespan(app):
    async with HTTP_CLIENTS:
        yield


mcp = FastMCP("resourcer", lifespan=server_lifespan)
NETWORK = RequestPolicy.from_env()
PARSER = ParsePolicy.from_env()
RESEARCH_ROOT = Path(
    os.environ.get("RESOURCER_RESEARCH_ROOT", str(Path.home() / "research"))
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0 Safari/537.36"
)


# ───────────────────────── helpers ─────────────────────────

def _clean_ddg_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        q = parse_qs(urlparse(href).query)
        if "uddg" in q:
            return q["uddg"][0]
    return href


def _parse_ddg(html: str, max_results: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for div in soup.select("div.result"):
        a = div.select_one("a.result__a")
        if not a:
            continue
        url = _clean_ddg_url(a.get("href", ""))
        title = a.get_text(strip=True)
        snip = div.select_one(".result__snippet")
        snippet = snip.get_text(" ", strip=True) if snip else ""
        if urlparse(url).scheme in {"http", "https"} and title and not any(it["url"] == url for it in out):
            out.append({"url": url, "title": title, "snippet": snippet, "source": "ddg"})
        if len(out) >= max_results:
            break
    return out


def _clean_bing_url(href: str) -> str:
    """Bing 把真实 URL 包在 /ck/a?...&u=a1<base64>&... 里，剥出来。"""
    if not href:
        return ""
    if "/ck/a" in href:
        m = re.search(r"[?&]u=a1([^&]+)", href)
        if m:
            payload = m.group(1)
            # urlsafe-base64，需补齐 padding
            payload += "=" * (-len(payload) % 4)
            try:
                return base64.urlsafe_b64decode(payload).decode("utf-8", errors="ignore")
            except Exception:
                pass
    return href


def _parse_bing(html: str, max_results: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.b_algo"):
        h2 = li.find("h2")
        a = h2.find("a") if h2 else None
        if not a:
            continue
        url = _clean_bing_url(a.get("href", ""))
        title = a.get_text(strip=True)
        p = li.select_one("p, .b_caption p")
        snippet = p.get_text(" ", strip=True) if p else ""
        if urlparse(url).scheme in {"http", "https"} and title and not any(it["url"] == url for it in out):
            out.append({"url": url, "title": title, "snippet": snippet, "source": "bing"})
        if len(out) >= max_results:
            break
    return out


class SearchEngineError(Exception):
    """An engine failed; this is different from a valid empty result page."""


@dataclass
class SearchOutcome:
    items: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    filtered_count: int = 0
    strategy: str = "fallback"
    include_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    deadline_exceeded: bool = False

    @property
    def status(self) -> str:
        if self.items:
            return "results"
        if self.failures:
            return "incomplete"
        return "filtered_empty" if self.filtered_count else "no_results"


def _parse_search_response(response: httpx.Response, engine: str, max_results: int) -> list[dict]:
    if response.status_code == 429:
        raise SearchEngineError("限流（HTTP 429）")
    if response.status_code in {202, 403}:
        raise SearchEngineError(f"访问受限或需要验证（HTTP {response.status_code}）")
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.select_one(".anomaly-modal, #challenge-form, #b_captcha, .g-recaptcha, #captcha"):
        raise SearchEngineError("访问受限或需要验证码")
    parser = _parse_ddg if engine == "ddg" else _parse_bing
    try:
        items = parser(response.text, max_results)
    except (TypeError, ValueError) as exc:
        raise SearchEngineError("解析失败：结果页面包含无效数据") from exc
    if items:
        return items
    empty_selector = ".no-results, .result--no-result, .no-results__message" if engine == "ddg" else "#b_results .b_no"
    if soup.select_one(empty_selector):
        return []
    raise SearchEngineError("解析失败：未识别结果或无结果标记，可能是页面改版或拦截")


async def _ddg(client: httpx.AsyncClient, query: str, max_results: int) -> list[dict]:
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    response = await NETWORK.http.request(client, "POST", url, headers={"User-Agent": UA},
                                          pace=lambda: NETWORK.wait_for_search("ddg"))
    return _parse_search_response(response, "ddg", max_results)


def _has_chinese(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)


async def _bing(client: httpx.AsyncClient, query: str, max_results: int, mkt: str) -> list[dict]:
    """使用 cn.bing.com，靠 mkt 切换语言偏好。"""
    url = f"https://cn.bing.com/search?q={quote_plus(query)}&count={max_results}&mkt={mkt}"
    accept_lang = "zh-CN,zh;q=0.9,en;q=0.8" if mkt.startswith("zh") else "en-US,en;q=0.9,zh;q=0.5"
    response = await NETWORK.http.request(client, "GET", url,
                                          headers={"User-Agent": UA, "Accept-Language": accept_lang},
                                          pace=lambda: NETWORK.wait_for_search("bing"))
    # Some regions redirect cn.bing.com/search to the www homepage, losing /search.
    # Retry the canonical search route once instead of parsing the homepage as results.
    if response.url.host in {"bing.com", "www.bing.com", "cn.bing.com"} and response.url.path in {"", "/"}:
        canonical_url = f"https://www.bing.com/search?q={quote_plus(query)}&count={max_results}&mkt={mkt}"
        response = await NETWORK.http.request(client, "GET", canonical_url,
                                              headers={"User-Agent": UA, "Accept-Language": accept_lang},
                                              pace=lambda: NETWORK.wait_for_search("bing"))
    return _parse_search_response(response, "bing", max_results)


async def _search(query: str, max_results: int, strategy: str = "fallback",
                  include_domains: list[str] | None = None,
                  exclude_domains: list[str] | None = None,
                  snapshot: SearchOutcome | None = None) -> SearchOutcome:
    """Route by language, enforce domains locally, optionally merge engines."""
    if strategy not in {"fallback", "merge"}:
        raise ValueError("strategy 必须是 fallback 或 merge")
    include = normalize_domains(include_domains)
    exclude = normalize_domains(exclude_domains)
    outcome = snapshot if snapshot is not None else SearchOutcome()
    outcome.strategy, outcome.include_domains, outcome.exclude_domains = strategy, include, exclude
    if not query.strip():
        outcome.failures.append("关键词不能为空")
        return outcome
    max_results = max(1, min(int(max_results), 50))
    candidate_count = min(50, max(10, max_results * 3)) if include or exclude else max_results
    chinese = _has_chinese(query)
    hints = []
    if include:
        sites = " OR ".join(f"site:{d}" for d in include)
        hints.append(f"({sites})" if len(include) > 1 else sites)
    hints.extend(f"-site:{d}" for d in exclude)
    engine_query = " ".join([*hints, query])
    async with HTTP_CLIENTS.lease() as c:
        if chinese:
            engines = [
                ("Bing/zh-CN", lambda: _bing(c, engine_query, candidate_count, "zh-CN")),
                ("DuckDuckGo", lambda: _ddg(c, engine_query, candidate_count)),
            ]
        else:
            engines = [
                ("DuckDuckGo", lambda: _ddg(c, engine_query, candidate_count)),
                ("Bing/en-US", lambda: _bing(c, engine_query, candidate_count, "en-US")),
            ]
            if strategy == "fallback":
                engines.append(("Bing/zh-CN", lambda: _bing(c, engine_query, candidate_count, "zh-CN")))

        async def attempt(name, fetch):
            failure = ""
            try:
                res = await wait_for_owned(fetch(), NETWORK.http.total_timeout)
                selected, rejected = select_results(res, name, include, exclude)
                return selected, {"engine": name, "status": "success", "returned": len(res),
                                  "accepted": len(selected), "filtered": rejected}, ""
            except (httpx.TimeoutException, asyncio.TimeoutError):
                failure = "请求超时"
            except HTTPPolicyError as exc:
                failure = str(exc)
            except httpx.HTTPStatusError as exc:
                failure = f"HTTP {exc.response.status_code}"
            except httpx.RequestError:
                failure = "网络连接失败"
            except SearchEngineError as exc:
                failure = str(exc)
            return [], {"engine": name, "status": "failed", "error": failure}, f"{name}：{failure}"

        groups = [[] for _ in engines]
        failures = ["" for _ in engines]
        attempts = [None for _ in engines]

        async def record(index, name, fetch):
            attempts[index] = {"engine": name, "status": "running"}
            outcome.attempts = [entry for entry in attempts if entry is not None]
            items, metadata, failure = await attempt(name, fetch)
            groups[index], attempts[index], failures[index] = items, metadata, failure
            outcome.attempts = [entry for entry in attempts if entry is not None]
            outcome.filtered_count = sum(entry.get("filtered", 0) for entry in outcome.attempts)
            outcome.failures = [error for error in failures if error]
            outcome.items = merge_results(groups, max_results)
            return items

        if strategy == "merge":
            tasks = [asyncio.create_task(record(index, name, fetch)) for index, (name, fetch) in enumerate(engines)]
            try:
                await asyncio.gather(*tasks)
            finally:
                await cancel_and_wait(tasks)
        else:
            for index, (name, fetch) in enumerate(engines):
                selected = await record(index, name, fetch)
                if selected:
                    break
    return outcome


def _search_notes(outcome: SearchOutcome) -> str:
    notes = list(outcome.failures)
    if outcome.filtered_count:
        notes.append(f"已排除 {outcome.filtered_count} 条不符合域名条件或 URL 无效的候选结果；仅检查本次引擎返回的候选。")
    if not notes:
        return ""
    return "\n\n搜索诊断：\n" + "\n".join(f"- {note}" for note in notes)


def _format_search(query: str, outcome: SearchOutcome) -> str:
    if not outcome.items and outcome.failures:
        return f"搜索未完成：无法确认 “{query}” 是否有相关结果。" + _search_notes(outcome)
    if outcome.status == "filtered_empty":
        return f"本次候选中没有符合筛选条件的结果：{query}" + _search_notes(outcome)
    return _format_results(query, outcome.items) + _search_notes(outcome)


def _search_payload(query: str, outcome: SearchOutcome) -> dict:
    return {
        "ok": bool(outcome.items) or not outcome.failures,
        "query": query, "search_status": outcome.status,
        "complete": not bool(outcome.failures), "strategy": outcome.strategy,
        "include_domains": outcome.include_domains, "exclude_domains": outcome.exclude_domains,
        "results": outcome.items, "diagnostics": outcome.failures,
        "attempts": outcome.attempts, "filtered_count": outcome.filtered_count,
        "deadline_exceeded": outcome.deadline_exceeded,
    }


def _unfinished_search(outcome: SearchOutcome, unfinished: Unfinished) -> SearchOutcome:
    outcome.deadline_exceeded = True
    message = "整批时间预算已用尽，搜索已中止" if unfinished.started else "整批时间预算已用尽，搜索尚未开始"
    outcome.failures.append(message)
    for attempt in outcome.attempts:
        if attempt["status"] == "running":
            attempt.update(status="deadline", error=message)
    return outcome


def _failed_search(outcome: SearchOutcome, error: Exception) -> SearchOutcome:
    message = f"查询失败：{type(error).__name__}"
    outcome.failures.append(message)
    for attempt in outcome.attempts:
        if attempt["status"] == "running":
            attempt.update(status="failed", error=message)
    return outcome


def _batch_page(url: str, result) -> dict:
    if isinstance(result, Unfinished):
        return {"url": url, "ok": False, "title": "", "text": "", "error_code": "batch_deadline",
                "started": result.started, "error": "整批时间预算已用尽，" + ("抓取已中止" if result.started else "抓取尚未开始")}
    if isinstance(result, Exception):
        return {"url": url, "ok": False, "title": "", "text": "", "error_code": "fetch_error",
                "error": f"抓取异常：{type(result).__name__}"}
    return result


def _batch_note(budget: BatchBudget) -> str:
    info = budget.metadata()
    if info["complete"] and not info["deadline_exceeded"]:
        return ""
    return (f"\n_整批时间预算 {info['time_budget_seconds']} 秒已用尽；保留已完成结果，"
            f"{info['timed_out']} 项已中止，{info['not_started']} 项尚未开始。_\n")


def _format_results(query: str, items: list[dict]) -> str:
    if not items:
        return f"未找到 “{query}” 的相关结果"
    lines = [f"# 搜索结果：{query}（{len(items)} 条）"]
    for i, it in enumerate(items, 1):
        src = ", ".join(dict.fromkeys(p["engine"] for p in it.get("provenance", []))) or it.get("source", "")
        lines.append(f"\n## {i}. {it['title']}  `[{src}]`")
        lines.append(f"<{it['url']}>")
        if it["snippet"]:
            lines.append(f"\n{it['snippet']}")
    return "\n".join(lines)


def _slice_page(result: dict, start_index: int, limit: int | None) -> dict:
    """Slice by Unicode code points; None means unlimited, zero means no body."""
    text = result["text"]
    total = len(text)
    start = min(start_index, total)
    end = total if limit is None else min(total, start + limit)
    return {
        **result, "text": text[start:end], "total_chars": total,
        "start_index": start, "end_index": end,
        "next_index": end if end < total else None,
        "truncated": end < total,
    }


async def _fetch_one(client: httpx.AsyncClient, url: str, max_chars: int,
                     start_index: int = 0, refresh: bool = False, content_format: str = "text") -> dict:
    if max_chars < 0 or start_index < 0:
        raise ValueError("max_chars 和 start_index 必须是非负整数")
    if content_format not in {"text", "markdown"}:
        raise ValueError("content_format 必须是 text 或 markdown")
    try:
        result = await wait_for_owned(
            NETWORK.fetch(url, lambda: _fetch_uncached(client, url), refresh=refresh), NETWORK.http.total_timeout)
    except asyncio.TimeoutError:
        result = {"url": url, "ok": False, "error": "抓取总时间预算已用尽（含排队与等待）",
                  "error_code": "deadline", "title": "", "text": ""}
    markdown = result.pop("_markdown", "")
    markdown_error = result.pop("_markdown_error", "")
    if content_format == "markdown":
        previous_error = result.get("error", "") if not result.get("ok") else ""
        result.update(text=markdown, ok=bool(markdown), error="" if markdown else markdown_error or previous_error or "无法提取 Markdown 正文")
    elif result.get("ok") and not result["text"]:
        result.update(ok=False, error=result.get("error") or "无法提取正文")
    result["content_format"] = content_format
    if result["ok"]:
        prefix = "markdown\0" if content_format == "markdown" else ""
        result["content_id"] = hashlib.sha256((prefix + result["text"]).encode("utf-8")).hexdigest()
        result["warnings"] = content_warnings(result["text"], result.get("title", ""))
        if markdown_error:
            result["warnings"].append("markdown_unavailable")
    return _slice_page(result, start_index, max_chars or None)


def _page_note(result: dict) -> str:
    if "total_chars" not in result:
        return ""
    note = f"\n\n_正文范围 [{result['start_index']}, {result['end_index']}) / 共 {result['total_chars']} 字符。_"
    if result["next_index"] is not None:
        version = f", expected_content_id={json.dumps(result['content_id'])}" if result.get("content_id") else ""
        formatting = ', content_format="markdown"' if result.get("content_format") == "markdown" else ""
        note += f"\n继续读取：fetch_page(url={json.dumps(result['url'], ensure_ascii=False)}, start_index={result['next_index']}{version}{formatting})"
    else:
        note += "\n_已到正文末尾。_"
    return note


def _tool_error(message: str, response_format: str) -> str:
    if response_format == "json":
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)
    return message


def _cache_note(result: dict) -> str:
    if result.get("cached"):
        return f"\n\n_正文来自进程内缓存（约 {result['cache_age_seconds']} 秒前抓取）。_"
    return ""


async def _fetch_uncached(client: httpx.AsyncClient, url: str) -> dict:
    try:
        r = await NETWORK.http.request(client, "GET", url, headers={"User-Agent": UA},
                                       pace=NETWORK.fetch_pacer.wait)
    except HTTPPolicyError as e:
        return {"url": url, "ok": False, "error": str(e), "error_code": e.code,
                "retry_after_seconds": e.retry_after_seconds, "title": "", "text": ""}
    except Exception as e:
        return {"url": url, "ok": False, "error": str(e) or type(e).__name__, "title": "", "text": ""}
    if r.status_code != 200:
        return {"url": url, "ok": False, "error": f"HTTP {r.status_code}", "error_code": "http_status",
                "request_info": r.extensions.get("resourcer", {}), "title": "", "text": ""}

    try:
        parsed = await PARSER.parse(r.text, str(r.url))
    except ParseFailure as exc:
        return {"url": url, "ok": False, "error": str(exc), "error_code": exc.code,
                "title": "", "text": "", "final_url": str(r.url),
                "request_info": r.extensions.get("resourcer", {})}
    return {
        **parsed, "url": url,
        "final_url": str(r.url),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "request_info": r.extensions.get("resourcer", {}),
    }


# ───────────────────────── tools: search ─────────────────────────

@mcp.tool()
@with_progress
async def web_search(
    query: str, max_results: int = 10,
    strategy: Literal["fallback", "merge"] = "fallback",
    include_domains: list[str] | None = None, exclude_domains: list[str] | None = None,
    response_format: Literal["text", "json"] = "text",
    ctx: Context = None,
) -> str:
    """通用网页搜索（中文优先 Bing，其他优先 DDG；失败自动切换并报告诊断）。

    Args:
        query: 关键词，可用 site: filetype: 高级语法
        max_results: 1-50
        strategy: fallback 首个有效引擎；merge 同时查询两个引擎并交替合并
        include_domains: 仅返回这些裸域名及其子域名；不填表示不限
        exclude_domains: 排除这些裸域名及其子域名，优先于 include_domains
        response_format: text 或 json；JSON 保留引擎、原始排名和失败诊断
    """
    max_results = max(1, min(int(max_results), 50))
    report(0, 1, "正在搜索网页")
    try:
        outcome = await _search(query, max_results, strategy, include_domains, exclude_domains)
    except ValueError as exc:
        return _tool_error(str(exc), response_format)
    report(1, 1, f"搜索已结束：{len(outcome.items)} 条来源" + ("，存在失败诊断" if outcome.failures else ""))
    if response_format == "json":
        return json.dumps(_search_payload(query, outcome), ensure_ascii=False)
    return _format_search(query, outcome)


@mcp.tool()
@with_progress
async def web_search_multi(
    queries: list[str], per_query: int = 8,
    strategy: Literal["fallback", "merge"] = "fallback",
    include_domains: list[str] | None = None, exclude_domains: list[str] | None = None,
    response_format: Literal["text", "json"] = "text",
    time_budget_seconds: float = 120,
    ctx: Context = None,
) -> str:
    """并发搜索多个 query，结果按 query 分组并去重。

    Args:
        queries: 关键词列表
        per_query: 每个 query 返回多少条（1-30）
        strategy: fallback 或 merge，作用于每个 query
        include_domains: 仅保留指定裸域名及其子域名
        exclude_domains: 排除指定裸域名及其子域名
        response_format: text 或 json；JSON 的 queries 保留每个查询关联的结果 URL
        time_budget_seconds: 整批搜索时间预算，默认 120 秒；大于 0 且不超过 600，到时返回部分结果
    """
    per_query = max(1, min(int(per_query), 30))
    if not queries:
        return _tool_error("queries 不能为空", response_format)
    try:
        budget = BatchBudget(time_budget_seconds)
        include_domains = normalize_domains(include_domains)
        exclude_domains = normalize_domains(exclude_domains)
        if strategy not in {"fallback", "merge"}:
            raise ValueError("strategy 必须是 fallback 或 merge")
    except ValueError as exc:
        return _tool_error(str(exc), response_format)

    snapshots = [SearchOutcome(strategy=strategy, include_domains=include_domains,
                               exclude_domains=exclude_domains) for _ in queries]
    report(0, len(queries), f"准备搜索 {len(queries)} 个查询")
    progress = CompletionCounter(len(queries), "查询", successful=lambda outcome: bool(outcome.items) or not outcome.failures)

    async def search_index(index):
        return await progress.run(_search(queries[index], per_query, strategy, include_domains, exclude_domains,
                                          snapshot=snapshots[index]))

    results = await budget.collect(list(range(len(queries))), search_index)

    seen_urls = set()
    blocks = []
    query_payloads = []
    all_groups = []
    total = 0
    for index, (q, res) in enumerate(zip(queries, results)):
        if isinstance(res, Unfinished):
            res = _unfinished_search(snapshots[index], res)
        if isinstance(res, Exception):
            res = _failed_search(snapshots[index], res)
        payload = _search_payload(q, res)
        payload.pop("results")
        payload["result_urls"] = [it.get("canonical_url") or url_identity(it["url"])[0] for it in res.items]
        query_payloads.append(payload)
        all_groups.append([
            {**it, "provenance": [{**p, "query": q} for p in it.get("provenance", [])]}
            for it in res.items
        ])
        unique = []
        for it in res.items:
            key = it.get("canonical_url") or url_identity(it["url"])[0]
            if key in seen_urls:
                continue
            seen_urls.add(key)
            unique.append(it)
        total += len(unique)
        if res.items and not unique:
            blocks.append(f"# {q}\n结果与前面的查询重复，已全部去重。" + _search_notes(res))
        else:
            blocks.append(_format_search(q, SearchOutcome(unique, res.failures, res.attempts, res.filtered_count)))
    if response_format == "json":
        combined = merge_results(all_groups, total)
        for item in combined:
            item["queries"] = list(dict.fromkeys(
                p["query"] for p in query_payloads if item["canonical_url"] in p["result_urls"]
            ))
        return json.dumps({"ok": all(p["ok"] for p in query_payloads),
                           "complete": all(p["complete"] for p in query_payloads),
                           "results": combined, "queries": query_payloads, "batch": budget.metadata()}, ensure_ascii=False)
    header = f"# 多查询搜索（{len(queries)} 个 query，去重后 {total} 条）\n"
    return header + _batch_note(budget) + "\n\n---\n\n".join(blocks)


@mcp.tool()
@with_progress
async def search_chinese(
    query: str, site: str = "zhihu", max_results: int = 10,
    strategy: Literal["fallback", "merge"] = "fallback",
    response_format: Literal["text", "json"] = "text",
    ctx: Context = None,
) -> str:
    """中文站点定向搜索（site: 提示并逐条校验返回链接的域名）。

    Args:
        query: 关键词
        site: zhihu | bilibili | weixin | jianshu | csdn | xueqiu
        max_results: 返回条数
        strategy: fallback 或 merge
        response_format: text 或 json
    """
    site_map = {
        "zhihu": "zhihu.com", "bilibili": "bilibili.com",
        "weixin": "mp.weixin.qq.com", "jianshu": "jianshu.com",
        "csdn": "blog.csdn.net", "xueqiu": "xueqiu.com",
    }
    domain = site_map.get(site.lower())
    if not domain:
        return _tool_error(f"不支持的 site：{site}（支持：{', '.join(site_map)}）", response_format)
    return await web_search(query, max_results, strategy, [domain], response_format=response_format, ctx=ctx)


# ───────────────────────── tools: fetch ─────────────────────────

@mcp.tool()
@with_progress
async def fetch_page(
    url: str, max_chars: int = 50000, start_index: int = 0,
    refresh: bool = False, response_format: Literal["text", "json"] = "text",
    expected_content_id: str = "",
    content_format: Literal["text", "markdown"] = "text",
    ctx: Context = None,
) -> str:
    """提取网页正文，支持分页续读、单次刷新及 JSON 元数据。

    Args:
        url: 目标 URL
        max_chars: 本次正文最多字符数；0 = 从起点读到末尾
        start_index: 正文字符偏移，从 0 开始；续读使用上次返回的 next_index
        refresh: 跳过已完成缓存；仍复用同 URL 正在进行的抓取
        response_format: text 返回可读文本；json 返回可解析 JSON 字符串
        expected_content_id: 可选的上次正文版本；不匹配时返回错误，防止续读混入更新后的正文
        content_format: text 提取纯文本，markdown 保留标题、代码、表格及正文链接；续读必须保持格式一致
    """
    if max_chars < 0 or start_index < 0:
        return _tool_error("max_chars 和 start_index 必须是非负整数", response_format)
    if content_format not in {"text", "markdown"}:
        return _tool_error("content_format 必须是 text 或 markdown", response_format)
    report(0, 1, "正在读取网页（含等待、下载与解析）")
    async with HTTP_CLIENTS.lease() as c:
        res = await _fetch_one(c, url, max_chars, start_index, refresh, content_format)
    if res["ok"] and expected_content_id and res.get("content_id") != expected_content_id:
        res = {**res, "ok": False, "text": "", "error": "正文版本已变化，请从 start_index=0 重新读取"}
    report(1, 1, "网页读取已结束：" + ("成功" if res["ok"] else "失败"))
    if response_format == "json":
        return json.dumps(res, ensure_ascii=False)
    if not res["ok"]:
        return f"抓取失败 ({res['error']})：{url}"
    head = f"# {res['title'] or url}\n<{url}>\n"
    tail = f"\n\n…（已截断到 {max_chars} 字符）" if res["truncated"] else ""
    return head + "\n" + res["text"] + tail + _page_note(res) + _cache_note(res)


@mcp.tool()
@with_progress
async def fetch_pages(urls: list[str], max_chars: int = 30000,
                      refresh: bool = False, response_format: Literal["text", "json"] = "text",
                      time_budget_seconds: float = 120,
                      content_format: Literal["text", "markdown"] = "text",
                      ctx: Context = None) -> str:
    """批量抓取正文；进程内最多 5 路网络抓取，共享限速与短时缓存。

    Args:
        urls: URL 列表
        max_chars: 每篇最长字符数；0 = 不截断
        refresh: 跳过已完成缓存
        response_format: text 或 json；JSON 中每页的 next_index 可传给 fetch_page 续读
        time_budget_seconds: 整批抓取时间预算，含分批排队；默认 120 秒，大于 0 且不超过 600
        content_format: text 或 markdown；与返回封装的 response_format 相互独立
    """
    if not urls:
        return _tool_error("urls 不能为空", response_format)
    if max_chars < 0:
        return _tool_error("max_chars 必须是非负整数", response_format)
    if content_format not in {"text", "markdown"}:
        return _tool_error("content_format 必须是 text 或 markdown", response_format)

    try:
        budget = BatchBudget(time_budget_seconds)
    except ValueError as exc:
        return _tool_error(str(exc), response_format)

    unique_urls = list(dict.fromkeys(urls))
    report(0, len(unique_urls), f"准备读取 {len(unique_urls)} 个不同网页")
    progress = CompletionCounter(len(unique_urls), "网页读取")
    async with HTTP_CLIENTS.lease() as c:
        unique_results = await budget.collect(unique_urls, lambda u: progress.run(_fetch_one(c, u, max_chars, refresh=refresh, content_format=content_format)),
                                             concurrency=FETCH_CONCURRENCY)
    by_url = {url: _batch_page(url, result) for url, result in zip(unique_urls, unique_results)}
    for page in by_url.values():
        page["content_format"] = content_format
    results = [dict(by_url[url]) for url in urls]

    ok = sum(1 for r in results if r["ok"])
    if response_format == "json":
        return json.dumps({"ok": ok == len(results), "successful": ok, "requested": len(urls), "pages": results,
                           "batch": budget.metadata(),
                           "unfinished_urls": [u for u in unique_urls if by_url[u].get("error_code") == "batch_deadline"]}, ensure_ascii=False)
    blocks = [f"# 批量抓取（{ok}/{len(urls)} 成功）", _batch_note(budget)]
    for r in results:
        if not r["ok"]:
            blocks.append(f"\n---\n## ❌ {r['url']}\n{r['error']}")
        else:
            tail = f"\n\n…（已截断到 {max_chars} 字符）" if r["truncated"] else ""
            blocks.append(f"\n---\n## {r['title'] or r['url']}\n<{r['url']}>\n\n{r['text']}{tail}" + _page_note(r) + _cache_note(r))
    return "\n".join(blocks)


# ───────────────────────── tools: deep research ─────────────────────────

@mcp.tool()
@with_progress
async def deep_research(
    query: str,
    num_results: int = 10,
    fetch_top_n: int = 6,
    max_chars_each: int = 20000,
    max_total_chars: int = 60000,
    response_format: Literal["text", "json"] = "text",
    strategy: Literal["fallback", "merge"] = "fallback",
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    time_budget_seconds: float = 120,
    content_format: Literal["text", "markdown"] = "text",
    ctx: Context = None,
) -> str:
    """搜索并抓取前 N 条正文，返回资料包供调用方总结；不自动写文件。

    Args:
        query: 调研主题
        num_results: 搜索结果数
        fetch_top_n: 抓取前几条的正文（≤ num_results）
        max_chars_each: 每篇正文截断长度；0 = 不截断
        max_total_chars: 本次返回的正文总字符预算；默认 60000，0 = 不限制。不含标题、索引和诊断
        response_format: text 或 json；JSON 保留来源、分页位置、抓取时间和诊断
        strategy: fallback 或 merge
        include_domains: 搜索结果仅保留指定裸域名及其子域名（不限制正文请求的 HTTP 跳转）
        exclude_domains: 搜索结果排除指定裸域名及其子域名
        time_budget_seconds: 搜索与正文抓取共用的整批时间预算，默认 120 秒；大于 0 且不超过 600
        content_format: text 或 markdown，作用于抓取正文；正文预算按所选格式的字符数计算
    """
    if min(max_chars_each, max_total_chars) < 0:
        return _tool_error("max_chars_each 和 max_total_chars 必须是非负整数", response_format)
    if content_format not in {"text", "markdown"}:
        return _tool_error("content_format 必须是 text 或 markdown", response_format)
    try:
        budget = BatchBudget(time_budget_seconds)
        include_domains = normalize_domains(include_domains)
        exclude_domains = normalize_domains(exclude_domains)
        if strategy not in {"fallback", "merge"}:
            raise ValueError("strategy 必须是 fallback 或 merge")
    except ValueError as exc:
        return _tool_error(str(exc), response_format)
    snapshot = SearchOutcome(strategy=strategy, include_domains=include_domains or [], exclude_domains=exclude_domains or [])
    report(0, None, "正在搜索调研来源")
    searched = await budget.collect([query], lambda q: _search(q, num_results, strategy, include_domains,
                                                              exclude_domains, snapshot=snapshot), concurrency=1)
    outcome = searched[0]
    if isinstance(outcome, Unfinished):
        outcome = _unfinished_search(snapshot, outcome)
    elif isinstance(outcome, ValueError):
        return _tool_error(str(outcome), response_format)
    elif isinstance(outcome, Exception):
        outcome = _failed_search(snapshot, outcome)
    items = outcome.items
    fetch_top_n = max(0, min(fetch_top_n, len(items)))
    if not isinstance(searched[0], Unfinished):
        report(1, 1 + fetch_top_n, f"搜索阶段已结束：{len(items)} 条来源，计划读取 {fetch_top_n} 页" +
               ("，存在失败诊断" if outcome.failures else ""))
    if not items:
        if response_format == "json":
            return json.dumps({
                **_search_payload(query, outcome), "pages": [], "batch": budget.metadata(), "unfinished_urls": [],
            }, ensure_ascii=False)
        return _format_search(query, outcome) + _batch_note(budget)

    top_urls = [it["url"] for it in items[:fetch_top_n]]
    progress = CompletionCounter(len(top_urls), "网页读取", offset=1)

    async with HTTP_CLIENTS.lease() as c:
        fetched = await budget.collect(top_urls, lambda u: progress.run(_fetch_one(c, u, 0, content_format=content_format)), concurrency=FETCH_CONCURRENCY)
    fetched = [_batch_page(url, result) for url, result in zip(top_urls, fetched)]
    for page in fetched:
        page["content_format"] = content_format

    remaining = max_total_chars if max_total_chars else None
    bounded = []
    for result in fetched:
        limit = max_chars_each if max_chars_each else None
        if remaining is not None:
            limit = min(limit, remaining) if limit is not None else remaining
        sliced = _slice_page(result, 0, limit)
        bounded.append(sliced)
        if remaining is not None:
            remaining -= len(sliced["text"])
    fetched = bounded
    fetched_by_url = {f["url"]: f for f in fetched}

    ok_count = sum(1 for f in fetched if f["ok"])
    returned_chars = sum(len(f["text"]) for f in fetched)
    if response_format == "json":
        return json.dumps({
            **_search_payload(query, outcome),
            "ok": ok_count == fetch_top_n, "pages": fetched,
            "requested_pages": fetch_top_n, "successful_pages": ok_count,
            "returned_body_chars": returned_chars, "max_total_chars": max_total_chars,
            "batch": budget.metadata(),
            "unfinished_urls": [f["url"] for f in fetched if f.get("error_code") == "batch_deadline"],
        }, ensure_ascii=False)
    out = [f"# 深度调研：{query}", f"_共 {len(items)} 条搜索结果，计划抓取 {fetch_top_n} 篇，成功 {ok_count} 篇_\n"]
    out.append(f"_本次返回正文 {returned_chars} 字符；总预算：{max_total_chars or '不限'}。_\n")
    out.append(_batch_note(budget))
    if outcome.failures or outcome.filtered_count:
        out.append(_search_notes(outcome))
    out.append("## 📋 结果索引\n")
    for i, it in enumerate(items, 1):
        result = fetched_by_url.get(it["url"])
        marker = ("✅" if result["ok"] else "❌") if result else "  "
        out.append(f"{marker} {i}. [{it['title']}]({it['url']})")
        if it["snippet"]:
            out.append(f"   > {it['snippet']}")

    out.append("\n## 📄 抓取正文\n")
    for i, url in enumerate(top_urls, 1):
        f = fetched_by_url.get(url, {})
        out.append(f"\n### {i}. {f.get('title') or url}")
        out.append(f"<{url}>\n")
        if f.get("ok"):
            out.append(f["text"])
            if not f["text"] and f["total_chars"]:
                out.append("_正文预算已用尽，请用下方续读入口单独读取。_")
            out.append(_page_note(f))
            if f.get("cached"):
                out.append(_cache_note(f))
        else:
            out.append(f"_抓取失败：{f.get('error', '未知')}_")
    return "\n".join(out)


# ───────────────────────── tools: local & save ─────────────────────────

@mcp.tool()
async def search_local(
    query: str,
    root: str,
    max_results: int = 50,
    include_ext: str = DEFAULT_EXTENSIONS,
    collection: str = "",
    tags: str = "",
    source_domain: str = "",
    exclude_dirs: str = DEFAULT_EXCLUDES,
    max_entries: int = 100000,
    max_file_bytes: int = 2097152,
    time_budget_seconds: float = 10,
    response_format: Literal["text", "json"] = "text",
) -> str:
    """本地文本搜索，可按 save_finding 保存的集合、标签和来源域名筛选。

    Args:
        query: 不区分大小写的子串；有元数据筛选时可留空以列出笔记
        root: 搜索根目录；只读取，不创建索引
        max_results: 最多匹配数，1–1000；关键词搜索按行计数，纯筛选按文件计数
        include_ext: 逗号分隔的扩展名；留空搜索所有普通文件
        collection: 原始集合名称，精确匹配、不区分大小写
        tags: 逗号分隔的标签，必须全部匹配、不区分大小写
        source_domain: 来源裸域名，包含子域名；不是正文关键词
        exclude_dirs: 排除的目录名，逗号分隔；覆盖默认值，留空不排除目录名
        max_entries: 遍历条目上限（文件和目录），1–1000000
        max_file_bytes: 单文件读取上限，默认 2 MiB，最大 16 MiB
        time_budget_seconds: 协作式扫描时限，默认 10 秒，最大 600 秒
        response_format: text 或 json；含扫描统计、跳过原因和完整性
    """
    try:
        if response_format not in {"text", "json"}:
            raise ValueError("response_format 必须是 text 或 json")
        result = await local_search(query, root, max_results, include_ext,
                                    collection=collection, tags=tags, source_domain=source_domain,
                                    exclude_dirs=exclude_dirs, max_entries=max_entries,
                                    max_file_bytes=max_file_bytes, time_budget_seconds=time_budget_seconds)
    except (ValueError, OSError) as exc:
        return _tool_error(str(exc), response_format)
    return json.dumps(result, ensure_ascii=False) if response_format == "json" else format_local(result)


@mcp.tool()
def save_finding(
    collection: str,
    title: str,
    content: str,
    source_url: str = "",
    tags: str = "",
) -> str:
    """把整理好的资料保存到 research collection。

    会写到：$RESOURCER_RESEARCH_ROOT/<collection>/<YYYY-MM-DD-HH-MM-SS>-<safe-title>.md
    （默认 ~/research）。带 frontmatter（title / collection / saved_at / source_url / tags）。

    Args:
        collection: 主题/项目名，例如 "mcp-protocol-2026"
        title: 资料标题
        content: 正文（markdown）
        source_url: 来源 URL（可选）
        tags: 逗号分隔的标签（可选）
    """
    safe_collection = re.sub(r"[^\w一-鿿\-_]+", "_", collection.strip()) or "misc"
    safe_title = re.sub(r"[^\w一-鿿\-_]+", "_", title.strip())[:80] or "untitled"
    ts = datetime.now()
    fname = f"{ts:%Y-%m-%d-%H-%M-%S}-{safe_title}.md"

    target_dir = RESEARCH_ROOT / safe_collection
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / fname

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    # JSON strings/lists are valid YAML scalars; quotes and newlines stay data.
    fm = ["---", f"title: {json.dumps(title, ensure_ascii=False)}",
          f"collection: {json.dumps(collection, ensure_ascii=False)}",
          f"saved_at: {json.dumps(ts.isoformat())}"]
    if source_url:
        fm.append(f"source_url: {json.dumps(source_url, ensure_ascii=False)}")
    if tag_list:
        fm.append("tags: " + json.dumps(tag_list, ensure_ascii=False))
    fm.append("---\n")

    suffix = 0
    while True:
        try:
            with target.open("x", encoding="utf-8") as output:
                output.write("\n".join(fm) + content)
            break
        except FileExistsError:
            suffix += 1
            target = target_dir / f"{Path(fname).stem}-{suffix}.md"

    count = sum(1 for _ in target_dir.glob("*.md"))
    return f"✅ 已保存：{target}\n（collection 中现共 {count} 篇）"


if __name__ == "__main__":
    mcp.run()
