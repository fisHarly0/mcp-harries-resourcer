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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus, parse_qs, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

from request_policy import RequestPolicy

mcp = FastMCP("resourcer")

DEFAULT_TIMEOUT = 25.0
FETCH_CONCURRENCY = 5
NETWORK = RequestPolicy.from_env()
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
    await NETWORK.wait_for_search("ddg")
    response = await client.post(url, headers={"User-Agent": UA})
    return _parse_search_response(response, "ddg", max_results)


def _has_chinese(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)


async def _bing(client: httpx.AsyncClient, query: str, max_results: int, mkt: str) -> list[dict]:
    """使用 cn.bing.com，靠 mkt 切换语言偏好。"""
    url = f"https://cn.bing.com/search?q={quote_plus(query)}&count={max_results}&mkt={mkt}"
    accept_lang = "zh-CN,zh;q=0.9,en;q=0.8" if mkt.startswith("zh") else "en-US,en;q=0.9,zh;q=0.5"
    await NETWORK.wait_for_search("bing")
    response = await client.get(url, headers={"User-Agent": UA, "Accept-Language": accept_lang})
    # Some regions redirect cn.bing.com/search to the www homepage, losing /search.
    # Retry the canonical search route once instead of parsing the homepage as results.
    if response.url.host in {"bing.com", "www.bing.com", "cn.bing.com"} and response.url.path in {"", "/"}:
        canonical_url = f"https://www.bing.com/search?q={quote_plus(query)}&count={max_results}&mkt={mkt}"
        await NETWORK.wait_for_search("bing")
        response = await client.get(canonical_url, headers={"User-Agent": UA, "Accept-Language": accept_lang})
    return _parse_search_response(response, "bing", max_results)


async def _search(query: str, max_results: int) -> SearchOutcome:
    """按 query 语言路由引擎，互相 fallback。"""
    if not query.strip():
        return SearchOutcome(failures=["关键词不能为空"])
    max_results = max(1, min(int(max_results), 50))
    chinese = _has_chinese(query)
    outcome = SearchOutcome()
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        if chinese:
            engines = [
                ("Bing/zh-CN", lambda: _bing(c, query, max_results, "zh-CN")),
                ("DuckDuckGo", lambda: _ddg(c, query, max_results)),
            ]
        else:
            engines = [
                ("DuckDuckGo", lambda: _ddg(c, query, max_results)),
                ("Bing/en-US", lambda: _bing(c, query, max_results, "en-US")),
                ("Bing/zh-CN", lambda: _bing(c, query, max_results, "zh-CN")),
            ]
        for name, fetch in engines:
            try:
                res = await fetch()
                if res:
                    outcome.items = res
                    return outcome
            except httpx.TimeoutException:
                outcome.failures.append(f"{name}：请求超时")
            except httpx.HTTPStatusError as exc:
                outcome.failures.append(f"{name}：HTTP {exc.response.status_code}")
            except httpx.RequestError:
                outcome.failures.append(f"{name}：网络连接失败")
            except SearchEngineError as exc:
                outcome.failures.append(f"{name}：{exc}")
    return outcome


def _search_notes(outcome: SearchOutcome) -> str:
    if not outcome.failures:
        return ""
    return "\n\n搜索诊断：\n" + "\n".join(f"- {failure}" for failure in outcome.failures)


def _format_search(query: str, outcome: SearchOutcome) -> str:
    if not outcome.items and outcome.failures:
        return f"搜索未完成：无法确认 “{query}” 是否有相关结果。" + _search_notes(outcome)
    return _format_results(query, outcome.items) + _search_notes(outcome)


def _format_results(query: str, items: list[dict]) -> str:
    if not items:
        return f"未找到 “{query}” 的相关结果"
    lines = [f"# 搜索结果：{query}（{len(items)} 条）"]
    for i, it in enumerate(items, 1):
        src = it.get("source", "")
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
                     start_index: int = 0, refresh: bool = False) -> dict:
    if max_chars < 0 or start_index < 0:
        raise ValueError("max_chars 和 start_index 必须是非负整数")
    result = await NETWORK.fetch(url, lambda: _fetch_uncached(client, url), refresh=refresh)
    return _slice_page(result, start_index, max_chars or None)


def _page_note(result: dict) -> str:
    if "total_chars" not in result:
        return ""
    note = f"\n\n_正文范围 [{result['start_index']}, {result['end_index']}) / 共 {result['total_chars']} 字符。_"
    if result["next_index"] is not None:
        version = f", expected_content_id={json.dumps(result['content_id'])}" if result.get("content_id") else ""
        note += f"\n继续读取：fetch_page(url={json.dumps(result['url'], ensure_ascii=False)}, start_index={result['next_index']}{version})"
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
        r = await client.get(url, headers={"User-Agent": UA})
    except Exception as e:
        return {"url": url, "ok": False, "error": str(e), "title": "", "text": ""}
    if r.status_code != 200:
        return {"url": url, "ok": False, "error": f"HTTP {r.status_code}", "title": "", "text": ""}

    try:
        text = trafilatura.extract(
            r.text, url=url,
            include_comments=False, include_tables=True, favor_recall=True,
        ) or ""
    except Exception:
        return {"url": url, "ok": False, "error": "正文解析失败", "title": "", "text": ""}
    title = ""
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
    except Exception:
        pass

    return {
        "url": url, "ok": bool(text), "error": "" if text else "无法提取正文",
        "title": title, "text": text,
        "final_url": str(r.url),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "content_id": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


# ───────────────────────── tools: search ─────────────────────────

@mcp.tool()
async def web_search(query: str, max_results: int = 10) -> str:
    """通用网页搜索（中文优先 Bing，其他优先 DDG；失败自动切换并报告诊断）。

    Args:
        query: 关键词，可用 site: filetype: 高级语法
        max_results: 1-50
    """
    max_results = max(1, min(int(max_results), 50))
    outcome = await _search(query, max_results)
    return _format_search(query, outcome)


@mcp.tool()
async def web_search_multi(queries: list[str], per_query: int = 8) -> str:
    """并发搜索多个 query，结果按 query 分组并去重。

    Args:
        queries: 关键词列表
        per_query: 每个 query 返回多少条（1-30）
    """
    per_query = max(1, min(int(per_query), 30))
    if not queries:
        return "queries 不能为空"

    results = await asyncio.gather(*[_search(q, per_query) for q in queries], return_exceptions=True)

    seen_urls = set()
    blocks = []
    total = 0
    for q, res in zip(queries, results):
        if isinstance(res, Exception):
            blocks.append(f"# {q}\n搜索失败：{res}")
            continue
        unique = []
        for it in res.items:
            if it["url"] in seen_urls:
                continue
            seen_urls.add(it["url"])
            unique.append(it)
        total += len(unique)
        if res.items and not unique:
            blocks.append(f"# {q}\n结果与前面的查询重复，已全部去重。" + _search_notes(res))
        else:
            blocks.append(_format_search(q, SearchOutcome(unique, res.failures)))
    header = f"# 多查询搜索（{len(queries)} 个 query，去重后 {total} 条）\n"
    return header + "\n\n---\n\n".join(blocks)


@mcp.tool()
async def search_chinese(query: str, site: str = "zhihu", max_results: int = 10) -> str:
    """中文站点定向搜索（site: 限定）。

    Args:
        query: 关键词
        site: zhihu | bilibili | weixin | jianshu | csdn | xueqiu
        max_results: 返回条数
    """
    site_map = {
        "zhihu": "zhihu.com", "bilibili": "bilibili.com",
        "weixin": "mp.weixin.qq.com", "jianshu": "jianshu.com",
        "csdn": "blog.csdn.net", "xueqiu": "xueqiu.com",
    }
    domain = site_map.get(site.lower())
    if not domain:
        return f"不支持的 site：{site}（支持：{', '.join(site_map)}）"
    return await web_search(f"site:{domain} {query}", max_results)


# ───────────────────────── tools: fetch ─────────────────────────

@mcp.tool()
async def fetch_page(
    url: str, max_chars: int = 50000, start_index: int = 0,
    refresh: bool = False, response_format: Literal["text", "json"] = "text",
    expected_content_id: str = "",
) -> str:
    """提取网页正文，支持分页续读、单次刷新及 JSON 元数据。

    Args:
        url: 目标 URL
        max_chars: 本次正文最多字符数；0 = 从起点读到末尾
        start_index: 正文字符偏移，从 0 开始；续读使用上次返回的 next_index
        refresh: 跳过已完成缓存；仍复用同 URL 正在进行的抓取
        response_format: text 返回可读文本；json 返回可解析 JSON 字符串
        expected_content_id: 可选的上次正文版本；不匹配时返回错误，防止续读混入更新后的正文
    """
    if max_chars < 0 or start_index < 0:
        return _tool_error("max_chars 和 start_index 必须是非负整数", response_format)
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        res = await _fetch_one(c, url, max_chars, start_index, refresh)
    if res["ok"] and expected_content_id and res.get("content_id") != expected_content_id:
        res = {**res, "ok": False, "text": "", "error": "正文版本已变化，请从 start_index=0 重新读取"}
    if response_format == "json":
        return json.dumps(res, ensure_ascii=False)
    if not res["ok"]:
        return f"抓取失败 ({res['error']})：{url}"
    head = f"# {res['title'] or url}\n<{url}>\n"
    tail = f"\n\n…（已截断到 {max_chars} 字符）" if res["truncated"] else ""
    return head + "\n" + res["text"] + tail + _page_note(res) + _cache_note(res)


@mcp.tool()
async def fetch_pages(urls: list[str], max_chars: int = 30000,
                      refresh: bool = False, response_format: Literal["text", "json"] = "text") -> str:
    """批量抓取正文；进程内最多 5 路网络抓取，共享限速与短时缓存。

    Args:
        urls: URL 列表
        max_chars: 每篇最长字符数；0 = 不截断
        refresh: 跳过已完成缓存
        response_format: text 或 json；JSON 中每页的 next_index 可传给 fetch_page 续读
    """
    if not urls:
        return _tool_error("urls 不能为空", response_format)
    if max_chars < 0:
        return _tool_error("max_chars 必须是非负整数", response_format)

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _bound_fetch(client, u):
        async with sem:
            return await _fetch_one(client, u, max_chars, refresh=refresh)

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        unique_urls = list(dict.fromkeys(urls))
        unique_results = await asyncio.gather(*[_bound_fetch(c, u) for u in unique_urls])
    by_url = dict(zip(unique_urls, unique_results))
    results = [dict(by_url[url]) for url in urls]

    ok = sum(1 for r in results if r["ok"])
    if response_format == "json":
        return json.dumps({"ok": ok == len(results), "successful": ok, "requested": len(urls), "pages": results}, ensure_ascii=False)
    blocks = [f"# 批量抓取（{ok}/{len(urls)} 成功）"]
    for r in results:
        if not r["ok"]:
            blocks.append(f"\n---\n## ❌ {r['url']}\n{r['error']}")
        else:
            tail = f"\n\n…（已截断到 {max_chars} 字符）" if r["truncated"] else ""
            blocks.append(f"\n---\n## {r['title'] or r['url']}\n<{r['url']}>\n\n{r['text']}{tail}" + _page_note(r) + _cache_note(r))
    return "\n".join(blocks)


# ───────────────────────── tools: deep research ─────────────────────────

@mcp.tool()
async def deep_research(
    query: str,
    num_results: int = 10,
    fetch_top_n: int = 6,
    max_chars_each: int = 20000,
    max_total_chars: int = 60000,
    response_format: Literal["text", "json"] = "text",
) -> str:
    """搜索并抓取前 N 条正文，返回资料包供调用方总结；不自动写文件。

    Args:
        query: 调研主题
        num_results: 搜索结果数
        fetch_top_n: 抓取前几条的正文（≤ num_results）
        max_chars_each: 每篇正文截断长度；0 = 不截断
        max_total_chars: 本次返回的正文总字符预算；默认 60000，0 = 不限制。不含标题、索引和诊断
        response_format: text 或 json；JSON 保留来源、分页位置、抓取时间和诊断
    """
    if min(max_chars_each, max_total_chars) < 0:
        return _tool_error("max_chars_each 和 max_total_chars 必须是非负整数", response_format)
    outcome = await _search(query, num_results)
    items = outcome.items
    if not items:
        if response_format == "json":
            return json.dumps({
                "ok": not bool(outcome.failures), "query": query,
                "search_status": "incomplete" if outcome.failures else "no_results",
                "results": [], "pages": [], "diagnostics": outcome.failures,
            }, ensure_ascii=False)
        return _format_search(query, outcome)

    fetch_top_n = max(0, min(fetch_top_n, len(items)))
    top_urls = [it["url"] for it in items[:fetch_top_n]]

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _bound_fetch(client, u):
        async with sem:
            return await _fetch_one(client, u, 0)

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        fetched = await asyncio.gather(*[_bound_fetch(c, u) for u in top_urls])

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
            "ok": ok_count == fetch_top_n, "query": query, "search_status": "results",
            "results": items, "pages": fetched, "diagnostics": outcome.failures,
            "requested_pages": fetch_top_n, "successful_pages": ok_count,
            "returned_body_chars": returned_chars, "max_total_chars": max_total_chars,
        }, ensure_ascii=False)
    out = [f"# 深度调研：{query}", f"_共 {len(items)} 条搜索结果，尝试抓取 {fetch_top_n} 篇，成功 {ok_count} 篇_\n"]
    out.append(f"_本次返回正文 {returned_chars} 字符；总预算：{max_total_chars or '不限'}。_\n")
    if outcome.failures:
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
def search_local(
    query: str,
    root: str,
    max_results: int = 50,
    include_ext: str = "txt,md,py,js,ts,json,html,htm,csv,yaml,yml,toml,gd,godot",
) -> str:
    """在本地目录递归全文搜索（不区分大小写，子串匹配）。

    Args:
        query: 关键词
        root: 根目录绝对路径
        max_results: 最多返回多少处匹配
        include_ext: 仅搜索的扩展名（逗号分隔，不带点）；留空则搜所有
    """
    root_path = Path(root)
    if not root_path.exists():
        return f"目录不存在：{root}"
    if not root_path.is_dir():
        return f"不是目录：{root}"

    exts = {f".{e.strip().lower()}" for e in include_ext.split(",") if e.strip()}
    needle = query.lower()
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist", "build", ".godot"}

    hits = []
    for path in root_path.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue
        if exts and path.suffix.lower() not in exts:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if needle in line.lower():
                hits.append({"path": str(path), "line": lineno, "text": line.strip()[:200]})
                if len(hits) >= max_results:
                    break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"在 {root} 中未找到 “{query}”"

    lines = [f"# 本地搜索：{query}", f"root: {root}", f"命中 {len(hits)} 处\n"]
    current = None
    for h in hits:
        if h["path"] != current:
            current = h["path"]
            lines.append(f"\n**{current}**")
        lines.append(f"  L{h['line']}: {h['text']}")
    return "\n".join(lines)


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
