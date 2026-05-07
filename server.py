"""MCP server: 资料查找助手 (resourcer) — 高强度版

工具：
  - web_search        : DDG 网页搜索，限流自动 fallback 到 Bing
  - web_search_multi  : 并发跑多个 query，结果合并去重
  - fetch_page        : 抓取单页正文
  - fetch_pages       : 并发批量抓取
  - deep_research     : 一站式 → 搜索 + 抓前 N 个正文 + 汇总
  - search_local      : 本地目录全文搜索
  - search_chinese    : 知乎/B站/微信公众号 定向搜索
  - save_finding      : 把整理好的资料写入 research collection 目录

通过 stdio 与 Claude Code 通信，所以禁止 print 到 stdout。
"""

import asyncio
import base64
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, unquote, parse_qs, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("resourcer")

DEFAULT_TIMEOUT = 25.0
FETCH_CONCURRENCY = 5
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
            return unquote(q["uddg"][0])
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
        if url and title:
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
        if url and title:
            out.append({"url": url, "title": title, "snippet": snippet, "source": "bing"})
        if len(out) >= max_results:
            break
    return out


async def _ddg(client: httpx.AsyncClient, query: str, max_results: int) -> list[dict]:
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    for attempt in range(2):
        r = await client.post(url, headers={"User-Agent": UA})
        if r.status_code == 200:
            items = _parse_ddg(r.text, max_results)
            if items:
                return items
        if attempt == 0:
            await asyncio.sleep(1.2)
    return []


def _has_chinese(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)


async def _bing(client: httpx.AsyncClient, query: str, max_results: int, mkt: str) -> list[dict]:
    """始终走 cn.bing.com（国内访问最稳），靠 mkt 切换语言偏好。"""
    url = f"https://cn.bing.com/search?q={quote_plus(query)}&count={max_results}&mkt={mkt}"
    accept_lang = "zh-CN,zh;q=0.9,en;q=0.8" if mkt.startswith("zh") else "en-US,en;q=0.9,zh;q=0.5"
    try:
        r = await client.get(url, headers={"User-Agent": UA, "Accept-Language": accept_lang})
    except Exception:
        return []
    if r.status_code != 200:
        return []
    return _parse_bing(r.text, max_results)


async def _search(query: str, max_results: int) -> list[dict]:
    """按 query 语言路由引擎，互相 fallback。"""
    chinese = _has_chinese(query)
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        if chinese:
            engines = [
                lambda: _bing(c, query, max_results, "zh-CN"),
                lambda: _ddg(c, query, max_results),
            ]
        else:
            engines = [
                lambda: _ddg(c, query, max_results),
                lambda: _bing(c, query, max_results, "en-US"),
                lambda: _bing(c, query, max_results, "zh-CN"),
            ]
        for fetch in engines:
            try:
                res = await fetch()
                if res:
                    return res
            except Exception:
                continue
    return []


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


async def _fetch_one(client: httpx.AsyncClient, url: str, max_chars: int) -> dict:
    try:
        r = await client.get(url, headers={"User-Agent": UA})
    except Exception as e:
        return {"url": url, "ok": False, "error": str(e), "title": "", "text": ""}
    if r.status_code != 200:
        return {"url": url, "ok": False, "error": f"HTTP {r.status_code}", "title": "", "text": ""}

    text = trafilatura.extract(
        r.text, url=url,
        include_comments=False, include_tables=True, favor_recall=True,
    ) or ""
    title = ""
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
    except Exception:
        pass

    truncated = False
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return {
        "url": url, "ok": bool(text), "error": "" if text else "无法提取正文",
        "title": title, "text": text, "truncated": truncated,
    }


# ───────────────────────── tools: search ─────────────────────────

@mcp.tool()
async def web_search(query: str, max_results: int = 10) -> str:
    """通用网页搜索（DDG 优先，限流时自动 fallback Bing）。

    Args:
        query: 关键词，可用 site: filetype: 高级语法
        max_results: 1-50
    """
    max_results = max(1, min(int(max_results), 50))
    items = await _search(query, max_results)
    return _format_results(query, items)


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
        for it in res:
            if it["url"] in seen_urls:
                continue
            seen_urls.add(it["url"])
            unique.append(it)
        total += len(unique)
        blocks.append(_format_results(q, unique))
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
async def fetch_page(url: str, max_chars: int = 50000) -> str:
    """抓取网页并提取正文（去广告/导航/页脚）。

    Args:
        url: 目标 URL
        max_chars: 最长字符数；0 = 不截断
    """
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        res = await _fetch_one(c, url, max_chars)
    if not res["ok"]:
        return f"抓取失败 ({res['error']})：{url}"
    head = f"# {res['title'] or url}\n<{url}>\n"
    tail = f"\n\n…（已截断到 {max_chars} 字符）" if res["truncated"] else ""
    return head + "\n" + res["text"] + tail


@mcp.tool()
async def fetch_pages(urls: list[str], max_chars: int = 30000) -> str:
    """并发批量抓取多个 URL 的正文。最多 5 路并发以免被封。

    Args:
        urls: URL 列表
        max_chars: 每篇最长字符数；0 = 不截断
    """
    if not urls:
        return "urls 不能为空"

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _bound_fetch(client, u):
        async with sem:
            return await _fetch_one(client, u, max_chars)

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        results = await asyncio.gather(*[_bound_fetch(c, u) for u in urls])

    ok = sum(1 for r in results if r["ok"])
    blocks = [f"# 批量抓取（{ok}/{len(urls)} 成功）"]
    for r in results:
        if not r["ok"]:
            blocks.append(f"\n---\n## ❌ {r['url']}\n{r['error']}")
        else:
            tail = f"\n\n…（已截断到 {max_chars} 字符）" if r["truncated"] else ""
            blocks.append(f"\n---\n## {r['title'] or r['url']}\n<{r['url']}>\n\n{r['text']}{tail}")
    return "\n".join(blocks)


# ───────────────────────── tools: deep research ─────────────────────────

@mcp.tool()
async def deep_research(
    query: str,
    num_results: int = 10,
    fetch_top_n: int = 6,
    max_chars_each: int = 20000,
) -> str:
    """一站式：搜索 → 抓取前 N 条结果的正文 → 汇总。适合整晚批量调研。

    Args:
        query: 调研主题
        num_results: 搜索结果数
        fetch_top_n: 抓取前几条的正文（≤ num_results）
        max_chars_each: 每篇正文截断长度；0 = 不截断
    """
    items = await _search(query, num_results)
    if not items:
        return f"未找到 “{query}” 的相关结果"

    fetch_top_n = max(0, min(fetch_top_n, len(items)))
    top_urls = [it["url"] for it in items[:fetch_top_n]]

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _bound_fetch(client, u):
        async with sem:
            return await _fetch_one(client, u, max_chars_each)

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        fetched = await asyncio.gather(*[_bound_fetch(c, u) for u in top_urls])

    fetched_by_url = {f["url"]: f for f in fetched}

    out = [f"# 深度调研：{query}", f"_共 {len(items)} 条搜索结果，抓取 {fetch_top_n} 篇正文_\n"]
    out.append("## 📋 结果索引\n")
    for i, it in enumerate(items, 1):
        marker = "✅" if i <= fetch_top_n else "  "
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
            if f.get("truncated"):
                out.append(f"\n\n…（已截断到 {max_chars_each} 字符）")
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
    fm = ["---", f"title: {title}", f"collection: {collection}",
          f"saved_at: {ts.isoformat()}"]
    if source_url:
        fm.append(f"source_url: {source_url}")
    if tag_list:
        fm.append("tags: [" + ", ".join(tag_list) + "]")
    fm.append("---\n")

    target.write_text("\n".join(fm) + content, encoding="utf-8")

    count = sum(1 for _ in target_dir.glob("*.md"))
    return f"✅ 已保存：{target}\n（collection 中现共 {count} 篇）"


if __name__ == "__main__":
    mcp.run()
