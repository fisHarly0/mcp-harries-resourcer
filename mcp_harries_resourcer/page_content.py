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


def _restore_markup(text: str, replacements: dict, protected_blocks: dict) -> tuple[str, int]:
    for token, markup in replacements.items():
        if token.startswith("RESOURCERLIST"):
            text = re.sub(r"[ \t\r\n]*" + token + r"[ \t\r\n]*", lambda _: markup, text)
        else:
            text = text.replace(token, markup)
    blocks = 0
    for marker, markup in protected_blocks.items():
        text, count = re.subn(marker + r"BEGIN.*?" + marker + "END",
                             lambda _: "\n\n" + markup + "\n\n", text, flags=re.DOTALL)
        blocks += count
        text = text.replace(marker + "BEGIN", "").replace(marker + "END", "")
    return text, blocks


def _render_lists(body, replacements: dict, protected_blocks: dict, list_starts: dict) -> int:
    """Render selected lists from the inside out, indenting by actual marker width."""
    blocks = 0
    for node in reversed(list(body.iter("list"))):
        # Table cells use the extractor's inline rendering, not block lists.
        if next(node.iterancestors("cell"), None) is not None:
            continue
        ordered = node.get("rend") == "ol"
        number = 1
        rendered = []
        for item in node:
            if item.tag != "item":
                continue
            content = deepcopy(item)
            content.tag, content.tail = "p", None
            for part in content.iter():
                for attr in ("text", "tail"):
                    text = getattr(part, attr) or ""
                    for marker, start in list_starts.items():
                        if marker in text:
                            number = start
                            text = text.replace(marker, "")
                    setattr(part, attr, text or None)
            text = xmltotxt(content, include_formatting=True).strip()
            text, restored = _restore_markup(text, replacements, protected_blocks)
            blocks += restored
            lines = text.strip().splitlines() or [""]
            # CommonMark accepts at most nine digits in a list marker; later
            # markers need not spell the displayed counter (the renderer adds it).
            prefix = f"{min(number, 999999999)}. " if ordered else "- "
            rendered.append(prefix + lines[0] + "".join(
                "\n" + (" " * len(prefix) + line if line else "") for line in lines[1:]))
            number += 1
        token = "RESOURCERLIST" + uuid4().hex
        following = node.getnext()
        separate = (following is not None and (following.text or "").startswith("RESOURCERLIST")
                    and following.text in replacements and not (node.tail or "").strip())
        replacements[token] = "\n\n" + "\n".join(rendered) + ("\n\n<!-- -->" if separate else "") + "\n\n"
        tail = node.tail
        node.clear()
        node.tag, node.text, node.tail = "p", token, tail
    return blocks


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

    list_starts = {}
    for ordered in soup.find_all("ol", start=True):
        first = ordered.find("li", recursive=False)
        try:
            start = int(ordered["start"])
        except (ValueError, TypeError):
            continue
        if first is not None and 0 <= start <= 999999999:
            marker = "RESOURCERSTART" + uuid4().hex
            list_starts[marker] = start
            first.insert(0, marker)

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
        if next(node.iterancestors("cell"), None) is not None:
            # GFM splits table cells before parsing code spans. Restored code
            # must escape pipes too; the renderer only sees our opaque token.
            text = text.replace("|", r"\|")
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
    code_blocks += _render_lists(body, replacements, protected_blocks, list_starts)
    markdown, restored = _restore_markup(xmltotxt(body, include_formatting=True), replacements, protected_blocks)
    code_blocks += restored
    for marker in list_starts:
        markdown = markdown.replace(marker, "")
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
