import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import server
from search_results import domain_matches, merge_results, normalize_domains, select_results, url_identity


def item(url, title="Result"):
    return {"url": url, "title": title, "snippet": "", "source": "ddg"}


class DomainTests(unittest.TestCase):
    def test_normalization_and_idn(self):
        self.assertEqual(normalize_domains([" EXAMPLE.ORG. ", "example.org", "例子.测试"]),
                         ["example.org", "xn--fsqu00a.xn--0zwm56d"])

    def test_invalid_domains_rejected(self):
        for value in ["", "https://example.org", "*.org", "x.org:443", "x.org/path",
                      "x.org?foo", "x.org#foo", "user@x.org", "a..org", "-a.org", "a_.org"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_domains([value])

    def test_domain_boundaries_and_exclusion_precedence(self):
        candidates = [item(url) for url in [
            "https://docs.example.org/guide", "https://example.org/guide",
            "https://notexample.org/guide", "https://example.org.evil.org/guide",
            "https://example.org@evil.org/guide", "https://evil.org/?next=example.org",
            "https://blocked.example.org/guide", "https://x.blocked.example.org/guide",
        ]]
        selected, rejected = select_results(candidates, "engine", ["example.org"], ["blocked.example.org"])
        self.assertEqual([r["url"] for r in selected], [r["url"] for r in candidates[:2]])
        self.assertEqual(rejected, 6)
        self.assertFalse(domain_matches("sub.127.0.0.1", "127.0.0.1"))

    def test_ambiguous_urls_are_rejected(self):
        for value in ["https://good.org\\@evil.org", "https://good.org\n.evil.org",
                      "https://user:pass@good.org", "https://good.org:bad/",
                      "https://good.org:65536/", "https://[bad", "javascript:foo", "https:///path"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                url_identity(value)

    def test_url_key_only_normalizes_conservative_equivalences(self):
        self.assertEqual(url_identity("HTTPS://EXAMPLE.ORG.:443#part")[0], "https://example.org/")
        self.assertEqual(url_identity("https://例子.测试/a")[1], "xn--fsqu00a.xn--0zwm56d")
        self.assertEqual(url_identity("http://[::1]:80/#x")[0], "http://[::1]/")
        keys = [url_identity(u)[0] for u in ["http://example.org/a", "https://example.org/a",
                "https://example.org/a/", "https://example.org/A", "https://example.org/a?id=1",
                "https://example.org/a?id=2", "https://example.org:8443/a"]]
        self.assertEqual(len(set(keys)), len(keys))

    def test_merge_fairness_and_provenance_after_limit(self):
        first, _ = select_results([item("https://example.org/a#x"), item("https://example.org/b")], "DDG", [], [])
        second, _ = select_results([item("https://other.org/c"), item("https://EXAMPLE.ORG:443/a#y")], "Bing", [], [])
        merged = merge_results([first, second], 2)
        self.assertEqual([r["canonical_url"] for r in merged], ["https://example.org/a", "https://other.org/c"])
        self.assertEqual([(p["engine"], p["rank"]) for p in merged[0]["provenance"]], [("DDG", 1), ("Bing", 2)])
        self.assertEqual(len(first[0]["provenance"]), 1)  # Inputs remain reusable.


class SearchControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_filter_empty_primary_tries_next_engine(self):
        with patch.object(server, "_ddg", AsyncMock(return_value=[item("https://wrong.org/")])) as ddg, \
                patch.object(server, "_bing", AsyncMock(return_value=[item("https://docs.python.org/guide")])) as bing:
            payload = json.loads(await server.web_search("asyncio", 1, include_domains=["python.org"], response_format="json"))
        self.assertEqual(payload["search_status"], "results")
        self.assertEqual(payload["filtered_count"], 1)
        self.assertTrue(payload["complete"])
        self.assertEqual(len(payload["attempts"]), 2)
        self.assertEqual(payload["results"][0]["provenance"][0]["engine"], "Bing/en-US")
        self.assertIn("site:python.org", ddg.call_args.args[1])
        self.assertEqual(ddg.call_args.args[2], 10)  # Bounded extra candidates for filtering.
        bing.assert_awaited_once()

    async def test_merged_partial_failure_preserves_usable_results(self):
        with patch.object(server, "_ddg", AsyncMock(side_effect=httpx.ReadTimeout("timeout"))), \
                patch.object(server, "_bing", AsyncMock(return_value=[item("https://example.org/")])) as bing:
            data = json.loads(await server.web_search("query", strategy="merge", response_format="json"))
        self.assertTrue(data["ok"])
        self.assertFalse(data["complete"])
        self.assertEqual(data["search_status"], "results")
        self.assertIn("超时", data["diagnostics"][0])
        bing.assert_awaited_once()  # One locale per engine in merge mode.

    async def test_merge_language_order_is_stable(self):
        with patch.object(server, "_ddg", AsyncMock(return_value=[item("https://ddg.example/a")])), \
                patch.object(server, "_bing", AsyncMock(return_value=[item("https://bing.example/a")])):
            english = await server._search("python", 2, "merge")
            chinese = await server._search("中文", 2, "merge")
        self.assertEqual([i["url"] for i in english.items], ["https://ddg.example/a", "https://bing.example/a"])
        self.assertEqual([i["url"] for i in chinese.items], ["https://bing.example/a", "https://ddg.example/a"])

    async def test_filtered_empty_is_not_global_no_results(self):
        with patch.object(server, "_ddg", AsyncMock(return_value=[item("https://wrong.org/")])), \
                patch.object(server, "_bing", AsyncMock(return_value=[])):
            data = json.loads(await server.web_search("query", include_domains=["example.org"], response_format="json"))
            text = await server.web_search("query", include_domains=["example.org"])
        self.assertEqual(data["search_status"], "filtered_empty")
        self.assertTrue(data["ok"])
        self.assertIn("本次候选", text)
        self.assertNotIn("未找到", text)

    async def test_filtered_plus_failure_is_incomplete(self):
        with patch.object(server, "_ddg", AsyncMock(return_value=[item("https://wrong.org/")])), \
                patch.object(server, "_bing", AsyncMock(side_effect=server.SearchEngineError("限流"))):
            data = json.loads(await server.web_search("query", include_domains=["example.org"], response_format="json"))
        self.assertEqual(data["search_status"], "incomplete")
        self.assertFalse(data["ok"])

    async def test_invalid_filter_never_sends_network_request(self):
        with patch.object(server.httpx, "AsyncClient") as client:
            for tool, args in [(server.web_search, {"query": "q"}),
                               (server.web_search_multi, {"queries": ["q"]}),
                               (server.deep_research, {"query": "q"})]:
                data = json.loads(await tool(**args, include_domains=["https://x.org"], response_format="json"))
                self.assertFalse(data["ok"])
                self.assertIn("裸域名", data["error"])
        client.assert_not_called()

    async def test_chinese_site_checks_actual_hostname(self):
        with patch.object(server, "_bing", AsyncMock(return_value=[item("https://zhihu.com.evil.org/a"),
                                                                  item("https://zhuanlan.zhihu.com/p/1")])), \
                patch.object(server, "_ddg", AsyncMock(return_value=[])):
            data = json.loads(await server.search_chinese("异步", response_format="json"))
        self.assertEqual([r["url"] for r in data["results"]], ["https://zhuanlan.zhihu.com/p/1"])
        self.assertEqual(data["include_domains"], ["zhihu.com"])

    async def test_chinese_empty_query_and_invalid_site_are_structured(self):
        with patch.object(server.httpx, "AsyncClient") as client:
            for args in [{"query": " "}, {"query": "q", "site": "missing"}]:
                data = json.loads(await server.search_chinese(**args, response_format="json"))
                self.assertFalse(data["ok"])
        client.assert_not_called()

    async def test_multi_dedup_keeps_query_membership_and_rank_provenance(self):
        async def search(q, *args, **kwargs):
            items, _ = select_results([item("https://example.org/a#" + q)], "DDG", [], [])
            return server.SearchOutcome(items)
        with patch.object(server, "_search", side_effect=search):
            data = json.loads(await server.web_search_multi(["first", "second"], response_format="json"))
        self.assertEqual(len(data["results"]), 1)
        result = data["results"][0]
        self.assertEqual(result["queries"], ["first", "second"])
        self.assertEqual([p["query"] for p in result["provenance"]], ["first", "second"])
        self.assertEqual([q["search_status"] for q in data["queries"]], ["results", "results"])
        self.assertEqual(data["queries"][1]["result_urls"], [result["canonical_url"]])

    async def test_multi_partial_failure_has_per_query_status(self):
        with patch.object(server, "_search", AsyncMock(side_effect=[server.SearchOutcome([item("https://a.org")]),
                                                                   RuntimeError("internal detail")])):
            data = json.loads(await server.web_search_multi(["first", "second"], response_format="json"))
        self.assertFalse(data["ok"])
        self.assertFalse(data["complete"])
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["queries"][1]["search_status"], "incomplete")

    async def test_research_fetches_only_allowed_results(self):
        async def fetch(client, url, max_chars, **options):
            return {"url": url, "title": "Guide", "text": "Allowed body", "ok": True}
        with patch.object(server, "_ddg", AsyncMock(return_value=[item("https://wrong.org/a"), item("https://docs.python.org/a")])), \
                patch.object(server, "_fetch_one", side_effect=fetch) as fetched:
            data = json.loads(await server.deep_research("asyncio", include_domains=["python.org"], response_format="json"))
        self.assertEqual(fetched.call_args.args[1], "https://docs.python.org/a")
        fetched.assert_awaited_once()
        self.assertEqual(data["filtered_count"], 1)
        self.assertIn("provenance", data["results"][0])

    async def test_research_filtered_empty_does_not_fetch(self):
        with patch.object(server, "_ddg", AsyncMock(return_value=[item("https://wrong.org/a")])), \
                patch.object(server, "_bing", AsyncMock(return_value=[])), \
                patch.object(server, "_fetch_one", AsyncMock()) as fetched:
            data = json.loads(await server.deep_research("q", include_domains=["python.org"], response_format="json"))
        self.assertEqual(data["search_status"], "filtered_empty")
        self.assertEqual(data["pages"], [])
        fetched.assert_not_called()
