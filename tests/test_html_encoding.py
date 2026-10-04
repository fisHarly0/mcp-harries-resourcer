import codecs
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

from mcp_harries_resourcer import server
from mcp_harries_resourcer.html_encoding import decode_html
from mcp_harries_resourcer.parse_policy import ParsePolicy


ARTICLE = (Path(__file__).parent / "fixtures" / "encoded_article.html").read_text(encoding="utf-8")
REAL_CLIENT = httpx.AsyncClient


class EncodingTests(unittest.TestCase):
    def test_meta_gb2312_uses_gb18030_including_four_byte_characters(self):
        html = '<meta charset="gb2312"><p>中文𠀀</p>'
        text, info = decode_html(html.encode("gb18030"), "text/html")
        self.assertEqual(text, html)
        self.assertEqual(info, {"encoding": "gb18030", "source": "meta", "had_errors": False})

    def test_bom_overrides_header_and_meta_and_is_removed(self):
        html = '<meta charset="gbk"><p>中文</p>'
        for bom, codec, expected in ((codecs.BOM_UTF8, "utf-8", "utf-8"),
                (codecs.BOM_UTF16_LE, "utf-16le", "utf-16le"),
                (codecs.BOM_UTF16_BE, "utf-16be", "utf-16be")):
            with self.subTest(codec=codec):
                text, info = decode_html(bom + html.encode(codec), "text/html; charset=windows-1252")
                self.assertEqual(text, html)
                self.assertEqual(info["source"], "bom")
                self.assertEqual(info["encoding"], expected)

    def test_header_wins_and_incorrect_explicit_header_is_not_guessed_away(self):
        html = '<meta charset="utf-8"><p>中文</p>'
        text, info = decode_html(html.encode("gbk"), 'text/html; charset="GBK"')
        self.assertEqual(text, html)
        self.assertEqual(info["source"], "http")
        damaged, info = decode_html(html.encode("gbk"), "text/html; charset=utf-8")
        self.assertIn("\ufffd", damaged)
        self.assertTrue(info["had_errors"])
        self.assertEqual(info["source"], "http")

    def test_meta_syntax_attribute_order_and_comments(self):
        for declaration in ('<META CHARSET=GBK>', '<meta charset="gbk"/>',
                '<meta content="text/html; charset=GBK" http-equiv="Content-Type">',
                "<meta HTTP-EQUIV='content-type' content='text/html;charset=gbk'>"):
            with self.subTest(declaration=declaration):
                html = '<!-- <meta charset="utf-8"> -->' + declaration + '<p>中文资料</p>'
                text, info = decode_html(html.encode("gbk"))
                self.assertEqual(text, html)
                self.assertEqual(info["source"], "meta")

    def test_fake_declarations_in_raw_text_and_attributes_do_not_win(self):
        for prefix in ('<script>"<meta charset=gbk>"</script>', '<script/>"<meta charset=gbk>"</script>',
                '<style>/* <meta charset=gbk> */</style>', '<textarea><meta charset=gbk></textarea>',
                '<title><meta charset=gbk></title>', '<div data-example="<meta charset=gbk>"></div>'):
            with self.subTest(prefix=prefix):
                html = prefix + '<meta charset=utf-8><p>中文</p>'
                text, info = decode_html(html.encode())
                self.assertEqual(text, html)
                self.assertEqual(info["encoding"], "utf-8")

    def test_unsupported_header_and_meta_can_use_later_valid_declaration(self):
        html = '<meta charset=utf-7><meta charset=gbk><p>中文</p>'
        text, info = decode_html(html.encode("gbk"), "text/html; charset=rot_13")
        self.assertEqual(text, html)
        self.assertEqual(info["source"], "meta")

    def test_script_examples_inside_text_containers_do_not_swallow_later_meta(self):
        for tag in ("textarea", "title", "xmp"):
            for ending in (">", "/>"):
                html = f'<{tag}{ending}<script>example</{tag}><meta charset=gbk><p>中文</p>'
                with self.subTest(tag=tag, ending=ending):
                    text, info = decode_html(html.encode("gbk"))
                    self.assertEqual(text, html)
                    self.assertEqual(info["source"], "meta")

    def test_non_web_codecs_and_replacement_labels_are_not_used(self):
        for label in ("utf-7", "unicode_escape", "base64_codec", "rot_13", "iso-2022-kr", "hz-gb-2312"):
            with self.subTest(label=label):
                text, info = decode_html(b"<p>text</p>", "text/html; charset=" + label)
                self.assertEqual(text, "<p>text</p>")
                self.assertEqual(info["source"], "default")

    def test_meta_requires_charset_or_content_type_pragma(self):
        html = '<meta content="text/html; charset=gbk"><p>中文</p>'
        self.assertEqual(decode_html(html.encode())[0], html)
        self.assertEqual(decode_html(html.encode())[1]["source"], "default")

    def test_first_supported_meta_and_first_duplicate_attribute_win(self):
        html = '<meta charset=utf-8 charset=gbk><meta charset=gbk><p>中文</p>'
        text, info = decode_html(html.encode())
        self.assertEqual(text, html)
        self.assertEqual(info["encoding"], "utf-8")

    def test_meta_scan_is_bounded_to_first_1024_bytes(self):
        declaration = b'<meta charset="gbk">'
        body = '<p>中文</p>'.encode("gbk")
        for end, recognized in ((1024, True), (1025, False), (1100, False)):
            with self.subTest(end=end):
                _, info = decode_html(b" " * (end - len(declaration)) + declaration + body)
                self.assertEqual(info["source"], "meta" if recognized else "default")
                self.assertEqual(info["had_errors"], not recognized)

    def test_meta_utf16_is_treated_as_utf8_and_user_defined_as_windows1252(self):
        html = '<meta charset=utf-16><p>中文</p>'
        self.assertEqual(decode_html(html.encode())[0], html)
        text, info = decode_html(b'<meta charset=x-user-defined><p>\x80</p>')
        self.assertIn("€", text)
        self.assertEqual(info["encoding"], "windows-1252")

    def test_legacy_encodings_and_web_latin1_alias(self):
        for label, codec, value in (("Big5", "big5", "繁體中文"),
                ("Shift_JIS", "shift_jis", "日本語"), ("EUC-KR", "euc_kr", "한국어"),
                ("iso-8859-1", "cp1252", "€ smart “quotes”")):
            with self.subTest(label=label):
                html = '<meta charset="' + label + '"><p>' + value + '</p>'
                text, info = decode_html(html.encode(codec))
                self.assertEqual(text, html)
                self.assertFalse(info["had_errors"])

    def test_utf8_default_invalid_bytes_and_literal_replacement_are_distinct(self):
        for body, expected_error in (("中文".encode(), False), (b"abc\xff", True), ("�".encode(), False)):
            with self.subTest(body=body):
                text, info = decode_html(body)
                self.assertEqual(info["source"], "default")
                self.assertEqual(info["had_errors"], expected_error)
        self.assertEqual(decode_html(b"")[0], "")

    def test_malformed_markup_does_not_crash_decoder(self):
        text, info = decode_html(b'<![broken]><p>text</p>')
        self.assertIn("text", text)
        self.assertEqual(info["source"], "default")


class EncodedWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for patcher in (patch.object(server, "NETWORK", server.RequestPolicy(search_rpm=0, fetch_rpm=0)),
                        patch.object(server, "PARSER", ParsePolicy())):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_original_bytes_survive_worker_and_cached_markdown_pagination(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=ARTICLE.encode("gb18030"))
        async with REAL_CLIENT(transport=httpx.MockTransport(respond)) as client:
            plain = await server._fetch_one(client, "https://example.org/page", 0)
            first = await server._fetch_one(client, "https://example.org/page", 41, content_format="markdown")
            rest = await server._fetch_one(client, "https://example.org/page", 0,
                                          start_index=first["next_index"], content_format="markdown")
        self.assertTrue(plain["ok"])
        self.assertEqual(plain["title"], "中文编码资料")
        self.assertIn("中文编码资料", plain["text"])
        self.assertIn('print("中文资料读取成功")', first["text"] + rest["text"])
        self.assertIn("中文参考文档", [ref["text"] for ref in first["references"]])
        self.assertEqual(plain["encoding_info"], {"encoding": "gb18030", "source": "meta", "had_errors": False})
        self.assertEqual(first["encoding_info"], plain["encoding_info"])
        self.assertTrue(first["cached"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("replacement_characters", plain["warnings"])

    async def test_search_html_is_decoded_before_title_and_snippet_extraction(self):
        for engine, html in (("bing", '<li class=b_algo><h2><a href="https://example.org">中文资料</a></h2><p>正确摘要</p></li>'),
                ("ddg", '<div class=result><a class=result__a href="https://example.org">中文资料</a><p class=result__snippet>正确摘要</p></div>')):
            with self.subTest(engine=engine):
                response = httpx.Response(200, content=('<meta charset=gb2312>' + html).encode("gbk"),
                                          request=httpx.Request("GET", "https://example.org"))
                result = await server._parse_search_response(response, engine, 1)
                self.assertEqual(result[0]["title"], "中文资料")
                self.assertEqual(result[0]["snippet"], "正确摘要")

    async def test_decode_errors_are_retained_even_outside_extracted_body(self):
        content = ARTICLE.replace('charset="gb2312"', 'charset="utf-8"').encode() + b'<!--\xff-->'
        async with REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content))) as client:
            result = await server._fetch_one(client, "https://example.org/bad", 0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["encoding_info"]["had_errors"])
        self.assertIn("decoding_errors", result["warnings"])
        self.assertNotIn("replacement_characters", result["warnings"])

    async def test_refresh_replaces_encoding_metadata_and_preserves_decoded_content_id(self):
        bodies = [ARTICLE.encode("gb18030"), ARTICLE.replace('charset="gb2312"', 'charset="utf-8"').encode()]
        async with REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=bodies.pop(0)))) as client:
            first = await server._fetch_one(client, "https://example.org/page", 0)
            latest = await server._fetch_one(client, "https://example.org/page", 0, refresh=True)
        self.assertEqual(first["content_id"], latest["content_id"])
        self.assertEqual(latest["encoding_info"]["encoding"], "utf-8")
        self.assertFalse(latest["cached"])
