import asyncio
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from mcp_harries_resourcer.page_content import content_warnings, extract_markdown, safe_link
from mcp_harries_resourcer import page_content
from mcp_harries_resourcer.request_policy import PageCache, RequestPolicy
from mcp_harries_resourcer import server


HTML = (Path(__file__).parent / "fixtures" / "technical_article.html").read_text(encoding="utf-8")
FINAL_URL = "https://example.org/docs/guides/start.html"
REAL_CLIENT = httpx.AsyncClient


class MarkdownExtractionTests(unittest.TestCase):
    def test_article_structure_and_references(self):
        result = extract_markdown(HTML, FINAL_URL)
        text = result["markdown"]
        self.assertIn("# Async worker guide", text)
        self.assertIn("## Run a worker", text)
        self.assertIn("`asyncio.run(main())`", text)
        self.assertIn("**original indentation**", text)
        self.assertIn("|---|---|", text)
        self.assertIn("| timeout | Maximum waiting time |", text)
        self.assertIn("- Keep the final URL", text)
        self.assertNotIn("Navigation only", text)
        self.assertNotIn("Footer only", text)
        self.assertNotIn("javascript:", text)
        self.assertEqual(result["structure"], {"code_blocks": 1, "tables": 1, "links": 2})
        self.assertEqual([r["url"] for r in result["references"]], [
            "https://example.org/docs/api/tasks.html#cancel", "https://example.net/guide?q=a&mode=b"])
        self.assertFalse(result["references_truncated"])

    def test_code_indentation_blank_lines_and_unicode_survive(self):
        text = extract_markdown(HTML, FINAL_URL)["markdown"]
        self.assertIn('```python\nasync def main():\n    for item in ["甲", "乙"]:\n        print(item)\n\nasyncio.run(main())\n```', text)
        self.assertNotIn("RESOURCERCODE", text)

    def test_markdown_is_deterministic_despite_internal_placeholders(self):
        self.assertEqual(extract_markdown(HTML, FINAL_URL), extract_markdown(HTML, FINAL_URL))

    def test_backtick_code_uses_a_longer_fence(self):
        html = HTML.replace('async def main():', 'literal = "```"\nasync def main():')
        text = extract_markdown(html, FINAL_URL)["markdown"]
        self.assertIn('````python\nliteral = "```"\nasync def main():', text)
        self.assertIn('asyncio.run(main())\n````', text)

    def test_single_line_pre_is_a_fenced_block(self):
        html = HTML.replace('async def main():\n    for item in ["甲", "乙"]:\n        print(item)\n\nasyncio.run(main())\n', 'print("one line")')
        self.assertIn('```python\nprint("one line")\n```', extract_markdown(html, FINAL_URL)["markdown"])

    def test_base_url_is_respected(self):
        html = HTML.replace("<head>", '<head><base href="https://static.example.net/v2/guides/">')
        result = extract_markdown(html, FINAL_URL)
        self.assertEqual(result["references"][0]["url"], "https://static.example.net/v2/api/tasks.html#cancel")

    def test_code_nested_in_lists_and_definitions_preserves_layout(self):
        block = '<pre><span>def nested():</span>\n    return 42\n\nprint(nested())</pre>'
        for wrapper in ['<ul><li>Run this example: {}</li></ul>',
                        '<dl><dt>Example</dt><dd>Run this example: {}</dd></dl>']:
            with self.subTest(wrapper=wrapper):
                html = HTML.replace('</article>', wrapper.format(block) + '</article>')
                result = extract_markdown(html, FINAL_URL)
                rendered = BeautifulSoup(MarkdownIt().render(result['markdown']), "html.parser")
                self.assertIn('def nested():\n    return 42\n\nprint(nested())\n',
                              [code.get_text() for code in rendered.select('pre > code')])
                self.assertNotIn('RESOURCERPRE', result['markdown'])
                self.assertEqual(result['structure']['code_blocks'], 2)

    def test_only_http_links_with_valid_authority_are_kept(self):
        for value in ["javascript:alert(1)", "data:text/plain,a", "mailto:test@example.org",
                      "https://user:password@example.org/", "https://[broken", "https://example.org:invalid/"]:
            with self.subTest(value=value):
                self.assertIsNone(safe_link(value, FINAL_URL))
        self.assertEqual(safe_link("../api/a page.html", FINAL_URL), "https://example.org/docs/api/a%20page.html")

    def test_reference_index_has_a_bound(self):
        extra = "<p>Related documentation: " + " ".join(
            f'<a href="https://example.net/{index}">Reference {index} about asynchronous workers</a>' for index in range(110)) + "</p>"
        result = extract_markdown(HTML.replace("</article>", extra + "</article>"), FINAL_URL)
        self.assertEqual(len(result["references"]), 100)
        self.assertTrue(result["references_truncated"])
        self.assertIn("https://example.net/109", result["markdown"])

    def test_warnings_are_hints_without_quality_scores(self):
        self.assertEqual(content_warnings("short \ufffd", ""), ["short_content", "replacement_characters", "missing_title"])
        self.assertEqual(content_warnings("Readable content " * 30, "Title"), [])


class MarkdownToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.html = HTML
        self.requests = []
        def respond(request):
            self.requests.append(str(request.url))
            if request.url.path == "/old":
                return httpx.Response(302, headers={"Location": FINAL_URL})
            return httpx.Response(200, text=self.html)
        patches = [
            patch.object(server, "PARSER", server.ParsePolicy()),
            patch.object(server, "NETWORK", RequestPolicy(fetch_rpm=0)),
            patch.object(server.httpx, "AsyncClient", side_effect=lambda **kw: REAL_CLIENT(
                transport=httpx.MockTransport(respond), trust_env=False)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def fetch(self, **options):
        return json.loads(await server.fetch_page(FINAL_URL, response_format="json", **options))

    async def test_formats_share_one_download_and_have_separate_versions(self):
        plain = await self.fetch(max_chars=0)
        md = await self.fetch(max_chars=0, content_format="markdown")
        self.assertTrue(plain["ok"] and md["ok"])
        self.assertTrue(md["cached"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(md["fetched_at"], plain["fetched_at"])
        self.assertNotEqual(md["content_id"], plain["content_id"])
        self.assertEqual(plain["content_id"], hashlib.sha256(plain["text"].encode()).hexdigest())
        self.assertNotIn("_markdown", md)
        self.assertNotIn("_markdown", plain)
        self.assertIn("```python", md["text"])

    async def test_wrong_format_version_cannot_continue(self):
        md = await self.fetch(content_format="markdown", max_chars=25)
        plain = await self.fetch(start_index=25, expected_content_id=md["content_id"])
        self.assertFalse(plain["ok"])
        self.assertEqual(plain["text"], "")
        self.assertIn("版本已变化", plain["error"])

    async def test_markdown_pagination_reconstructs_exact_document(self):
        full = await self.fetch(content_format="markdown", max_chars=0)
        parts, offset = [], 0
        while offset is not None:
            part = await self.fetch(content_format="markdown", max_chars=97, start_index=offset,
                                    expected_content_id=full["content_id"])
            self.assertTrue(part["ok"])
            parts.append(part["text"])
            offset = part["next_index"]
        self.assertEqual("".join(parts), full["text"])
        self.assertEqual(len(self.requests), 1)

    async def test_refresh_replaces_both_formats_together(self):
        old = await self.fetch(content_format="markdown")
        self.html = HTML.replace("Async worker guide", "Updated worker guide")
        refreshed = await self.fetch(refresh=True)
        md = await self.fetch(content_format="markdown")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(md["cached"])
        self.assertIn("Updated worker guide", md["text"])
        self.assertNotEqual(md["content_id"], old["content_id"])
        self.assertEqual(md["fetched_at"], refreshed["fetched_at"])

    async def test_redirected_page_resolves_links_against_final_url(self):
        data = json.loads(await server.fetch_page("https://example.org/old", content_format="markdown", response_format="json"))
        self.assertEqual(data["final_url"], FINAL_URL)
        self.assertIn("https://example.org/docs/api/tasks.html#cancel", data["text"])
        self.assertEqual(data["references"][0]["url"], "https://example.org/docs/api/tasks.html#cancel")

    async def test_concurrent_formats_share_inflight_download(self):
        values = await asyncio.gather(self.fetch(), self.fetch(content_format="markdown"))
        self.assertTrue(all(value["ok"] for value in values))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([v["content_format"] for v in values], ["text", "markdown"])

    async def test_markdown_failure_does_not_silently_return_plain_text(self):
        # Failure of one format is a pure extraction contract; process transport
        # and crashes are covered separately by the parser policy tests.
        async def parse(html, url):
            with patch.object(page_content, "extract_markdown", side_effect=ValueError("bad format")):
                return page_content.extract_page(html, url)
        with patch.object(server.PARSER, "parse", side_effect=parse):
            plain = await self.fetch()
            md = await self.fetch(content_format="markdown")
        self.assertTrue(plain["ok"])
        self.assertIn("markdown_unavailable", plain["warnings"])
        self.assertFalse(md["ok"])
        self.assertEqual(md["text"], "")
        self.assertIn("Markdown", md["error"])

    async def test_text_mode_continuation_preserves_markdown_parameter(self):
        text = await server.fetch_page(FINAL_URL, content_format="markdown", max_chars=30)
        self.assertIn('content_format="markdown"', text)
        self.assertIn("expected_content_id=", text)

    async def test_batch_and_research_pass_format_and_keep_character_budget(self):
        batch = json.loads(await server.fetch_pages([FINAL_URL], content_format="markdown", response_format="json"))
        self.assertIn("```python", batch["pages"][0]["text"])
        items = [{"url": FINAL_URL, "title": "Guide", "snippet": "", "source": "ddg"}]
        with patch.object(server, "_search", AsyncMock(return_value=server.SearchOutcome(items))):
            research = json.loads(await server.deep_research("q", content_format="markdown", max_total_chars=30, response_format="json"))
        self.assertEqual(research["returned_body_chars"], 30)
        self.assertEqual(research["pages"][0]["content_format"], "markdown")
        self.assertEqual(research["pages"][0]["next_index"], 30)
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_format_does_not_send_requests(self):
        for tool, args in [(server.fetch_page, {"url": FINAL_URL}),
                           (server.fetch_pages, {"urls": [FINAL_URL]}), (server.deep_research, {"query": "q"})]:
            value = json.loads(await tool(**args, content_format="invalid", response_format="json"))
            self.assertFalse(value["ok"])
        self.assertEqual(self.requests, [])


class FormatCacheTests(unittest.TestCase):
    def test_cache_budget_counts_markdown_and_reference_content(self):
        cache = PageCache(max_bytes=100)
        value = {"ok": True, "title": "T", "text": "short", "_markdown": "x" * 100}
        cache.put("url", value)
        self.assertIsNone(cache.get("url"))
        value["_markdown"] = "small"
        value["references"] = [{"url": "https://example.org/" + "x" * 100, "text": "source"}]
        cache.put("url", value)
        self.assertIsNone(cache.get("url"))

    def test_reference_metadata_is_copied_on_put_and_get(self):
        cache = PageCache()
        value = {"ok": True, "text": "body", "references": [{"url": "https://example.org"}]}
        cache.put("url", value)
        value["references"][0]["url"] = "changed"
        first = cache.get("url")
        self.assertEqual(first["references"][0]["url"], "https://example.org")
        first["references"].clear()
        self.assertEqual(len(cache.get("url")["references"]), 1)
