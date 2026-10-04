"""Pure HTML search extraction, usable without starting the MCP server."""
import base64
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


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


def _search_text(element) -> str:
    # Preserve spaces around inline highlights without splitting identifiers or
    # adding spaces before punctuation. HTML line breaks still separate words.
    for br in element.find_all("br"):
        br.replace_with(" ")
    return " ".join(element.get_text().split())


def _parse_ddg(html: str | BeautifulSoup, max_results: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser") if isinstance(html, str) else html
    out = []
    for div in soup.select("div.result"):
        a = div.select_one("a.result__a")
        if not a:
            continue
        url = _clean_ddg_url(a.get("href", ""))
        title = _search_text(a)
        snip = div.select_one(".result__snippet")
        snippet = _search_text(snip) if snip else ""
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


def _parse_bing(html: str | BeautifulSoup, max_results: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser") if isinstance(html, str) else html
    out = []
    for li in soup.select("li.b_algo"):
        h2 = li.find("h2")
        a = h2.find("a") if h2 else None
        if not a:
            continue
        url = _clean_bing_url(a.get("href", ""))
        title = _search_text(a)
        p = li.select_one("p, .b_caption p")
        snippet = _search_text(p) if p else ""
        if urlparse(url).scheme in {"http", "https"} and title and not any(it["url"] == url for it in out):
            out.append({"url": url, "title": title, "snippet": snippet, "source": "bing"})
        if len(out) >= max_results:
            break
    return out


class SearchEngineError(Exception):
    """An engine failed; this is different from a valid empty result page."""


def parse_search(html: str, engine: str, max_results: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(".anomaly-modal, #challenge-form, #b_captcha, .g-recaptcha, #captcha"):
        raise SearchEngineError("访问受限或需要验证码")
    parser = _parse_ddg if engine == "ddg" else _parse_bing
    try:
        items = parser(soup, max_results)
    except (TypeError, ValueError) as exc:
        raise SearchEngineError("解析失败：结果页面包含无效数据") from exc
    if items:
        return items
    empty_selector = ".no-results, .result--no-result, .no-results__message" if engine == "ddg" else "#b_results .b_no"
    if soup.select_one(empty_selector):
        return []
    raise SearchEngineError("解析失败：未识别结果或无结果标记，可能是页面改版或拦截")
