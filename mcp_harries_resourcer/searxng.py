"""Optional SearXNG JSON adapter; no public instance or implicit fallback."""
from dataclasses import dataclass, field
import os
from urllib.parse import urlencode, urlsplit

import httpx

from .search_results import url_identity


class SearXNGError(Exception):
    """The configured instance did not return a usable search response."""


@dataclass
class SearXNGReply:
    items: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unresponsive_engines: list[dict] = field(default_factory=list)
    invalid_results: int = 0


def validate_backend(backend: str, strategy: str) -> str | None:
    if backend not in {"builtin", "searxng"}:
        raise ValueError("backend 必须是 builtin 或 searxng")
    if strategy not in {"fallback", "merge"}:
        raise ValueError("strategy 必须是 fallback 或 merge")
    if backend == "builtin":
        return None
    if strategy != "fallback":
        raise ValueError("SearXNG 由实例负责聚合，请保留 strategy=fallback；不支持 builtin 的 merge 策略")
    value = os.environ.get("RESOURCER_SEARXNG_URL", "").strip()
    if not value:
        raise ValueError("使用 searxng 前请配置 RESOURCER_SEARXNG_URL，并在实例中启用 JSON 搜索")
    try:
        normalized, _ = url_identity(value)
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or "?" in value or "#" in value:
            raise ValueError("query/fragment")
        # Force HTTPX validation before leasing a client or sending any query.
        httpx.URL(normalized)
    except (ValueError, UnicodeError, httpx.InvalidURL) as exc:
        raise ValueError("RESOURCER_SEARXNG_URL 必须是无账号、查询参数和片段的 HTTP(S) 实例地址") from exc
    endpoint = normalized.rstrip("/")
    return endpoint if endpoint.endswith("/search") else endpoint + "/search"


def _short(value: str) -> str:
    return " ".join(value.split())[:256]


def parse_response(response: httpx.Response) -> SearXNGReply:
    if response.status_code == 403:
        raise SearXNGError("HTTP 403：请检查实例是否启用 search.formats 中的 json，以及访问权限")
    if response.status_code in {202, 429}:
        raise SearXNGError(f"实例限流或需要验证（HTTP {response.status_code}）")
    response.raise_for_status()
    try:
        data = response.json()
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SearXNGError("实例未返回有效 JSON，请检查地址、JSON 配置或访问限制") from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise SearXNGError("JSON 缺少有效的 results 列表")
    reply = SearXNGReply()
    # One page, at most 50 candidates. Ignore optional answer/image/infobox data.
    for rank, row in enumerate(data["results"][:50], 1):
        if (not isinstance(row, dict) or not isinstance(row.get("url"), str) or
                not isinstance(row.get("title"), str) or not row["title"].strip()):
            reply.invalid_results += 1
            continue
        engines = row.get("engines", [])
        if not isinstance(engines, list):
            engines = []
        if not engines and isinstance(row.get("engine"), str):
            engines = [row["engine"]]
        engines = list(dict.fromkeys(_short(e) for e in engines[:100] if isinstance(e, str) and e.strip()))
        reply.items.append({"url": row["url"], "title": " ".join(row["title"].split()),
                            "snippet": row.get("content") if isinstance(row.get("content"), str) else "",
                            "source": "searxng", "_rank": rank, "_engines": engines})
    if reply.invalid_results:
        reply.warnings.append(f"跳过 {reply.invalid_results} 条结构无效的搜索结果")
    errors = data.get("unresponsive_engines", [])
    if not isinstance(errors, list):
        reply.warnings.append("unresponsive_engines 格式无效，无法确认上游完整性")
    else:
        for entry in errors[:100]:
            if (not isinstance(entry, list) or len(entry) != 2 or
                    not all(isinstance(value, str) for value in entry)):
                reply.warnings.append("存在无法识别的上游错误记录")
                continue
            engine, error = map(_short, entry)
            reply.unresponsive_engines.append({"engine": engine, "error": error})
            reply.warnings.append(f"上游 {engine}：{error}")
        if len(errors) > 100:
            reply.warnings.append("上游错误记录超过 100 条，诊断已截断")
    return reply


async def search(client, network, endpoint: str, query: str) -> SearXNGReply:
    url = endpoint + "?" + urlencode({"q": query, "format": "json", "categories": "general", "pageno": 1})
    response = await network.http.request(
        client, "GET", url, headers={"Accept": "application/json"},
        pace=lambda: network.wait_for_search("searxng"), follow_redirects=False)
    return parse_response(response)
