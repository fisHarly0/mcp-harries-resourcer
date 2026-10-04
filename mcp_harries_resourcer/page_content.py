"""Readable Markdown and citations from the article selected by Trafilatura."""
from copy import deepcopy
import re
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
import trafilatura
from trafilatura.xml import xmltotxt


def safe_link(href: str, base_url: str) -> str | None:
    try:
        url = urljoin(base_url, href.strip())
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or
                any(ord(c) < 32 or c == "\\" for c in url)):
            return None
        parts.port  # Validate malformed/out-of-range ports.
        return quote(url, safe=":/?#[]@!$&'()*+,;=%-._~")
    except (ValueError, UnicodeError):
        return None


def _code_markup(text: str, block: bool, language: str = "") -> str:
    width = max((len(match) for match in re.findall(r"`+", text)), default=0) + 1
    fence = "`" * max(3 if block else 1, width)
    if block:
        return f"{fence}{language}\n{text}" + ("" if text.endswith("\n") else "\n") + fence
    padding = " " if text.startswith(("`", " ")) or text.endswith(("`", " ")) else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def extract_markdown(html: str, final_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    base = final_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = safe_link(base_tag["href"], final_url) or final_url
    for anchor in soup.find_all("a", href=True):
        target = safe_link(anchor["href"], base)
        if target:
            anchor["href"] = target
        else:
            del anchor["href"]

    protected_blocks = {}
    for pre in soup.find_all("pre"):
        text = pre.get_text()
        language = ""
        code = pre.find("code")
        for name in (code or pre).get("class", []):
            if name.startswith("language-") and re.fullmatch(r"[A-Za-z0-9_+.-]+", name[9:]):
                language = name[9:]
                break
        marker = "RESOURCERPRE" + uuid4().hex
        protected_blocks[marker] = _code_markup(text, True, language)
        # Keep the original text for article selection, but bracket it before
        # extraction can flatten <pre> nested in a list or definition item.
        pre.clear()
        pre.append(f"{marker}BEGIN\n{text}\n{marker}END")

    doc = trafilatura.bare_extraction(
        str(soup), url=final_url, output_format="markdown", include_links=True,
        include_formatting=True, include_comments=False, include_tables=True, favor_recall=True,
    )
    if doc is None or doc.body is None:
        return {"markdown": "", "references": [], "references_truncated": False,
                "structure": {"code_blocks": 0, "tables": 0, "links": 0}}
    body = deepcopy(doc.body)
    references, seen = [], set()
    for node in body.iter("ref"):
        target = safe_link(node.get("target", ""), base) if node.get("target") else None
        if not target:
            node.tag = "span"
            node.attrib.clear()
            continue
        node.set("target", target)
        if target not in seen:
            seen.add(target)
            if len(references) < 100:
                references.append({"url": target, "text": " ".join(node.itertext()).strip()[:300]})

    replacements = {}
    code_blocks = 0
    for node in list(body.iter("code")):
        text = "".join(node.itertext())
        parent = node.getparent()
        if parent is None:  # A nested <code> was already consumed with its outer block.
            continue
        if any(marker in text for marker in protected_blocks):
            continue
        block = parent.tag not in {"p", "head", "item", "cell", "hi", "ref"} or "\n" in text
        token = "RESOURCERCODE" + uuid4().hex
        replacements[token] = _code_markup(text, block)
        for child in list(node):
            node.remove(child)
        node.tag = "p" if block else "span"
        node.attrib.clear()
        node.text = token
        code_blocks += int(block)
    tables = sum(1 for _ in body.iter("table"))
    # Restore the protected blocks ourselves, without the renderer's extra fences.
    for node in body.iter("code"):
        if any(marker in "".join(node.itertext()) for marker in protected_blocks):
            node.tag = "span"
    markdown = xmltotxt(body, include_formatting=True)
    for token, markup in replacements.items():
        markdown = markdown.replace(token, markup)
    for marker, markup in protected_blocks.items():
        markdown, count = re.subn(marker + r"BEGIN.*?" + marker + "END",
                                  lambda _: "\n\n" + markup + "\n\n", markdown, flags=re.DOTALL)
        code_blocks += count
        markdown = markdown.replace(marker + "BEGIN", "").replace(marker + "END", "")
    return {"markdown": markdown.strip(), "references": references,
            "references_truncated": len(seen) > len(references),
            "structure": {"code_blocks": code_blocks, "tables": tables, "links": len(seen)}}


def content_warnings(text: str, title: str) -> list[str]:
    warnings = []
    if len(text.strip()) < 200:
        warnings.append("short_content")
    if "\ufffd" in text:
        warnings.append("replacement_characters")
    if not title:
        warnings.append("missing_title")
    return warnings


def extract_page(html: str, url: str) -> dict:
    """Extract both formats together; called only inside the parser worker in production."""
    try:
        text = trafilatura.extract(html, url=url, include_comments=False,
                                   include_tables=True, favor_recall=True) or ""
    except Exception:
        text, text_error = "", "正文解析失败"
    else:
        text_error = "" if text else "无法提取正文"
    try:
        formatted = extract_markdown(html, url)
        markdown_error = "" if formatted["markdown"] else "无法提取 Markdown 正文"
    except Exception:
        formatted = {"markdown": "", "references": [], "references_truncated": False, "structure": {}}
        markdown_error = "Markdown 正文解析失败"
    title = ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
    except Exception:
        pass
    return {"ok": bool(text or formatted["markdown"]), "error": text_error,
            "title": title, "text": text, "_markdown": formatted["markdown"],
            "_markdown_error": markdown_error, "references": formatted["references"],
            "references_truncated": formatted["references_truncated"], "structure": formatted["structure"],
            "extractor": "trafilatura", "extractor_version": trafilatura.__version__}
