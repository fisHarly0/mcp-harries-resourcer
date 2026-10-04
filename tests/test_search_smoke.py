import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import search_smoke


CASE = {"id": "asyncio", "language": "zh", "query": "Python asyncio 官方文档",
        "include_domains": ["docs.python.org"],
        "reference_urls": ["https://docs.python.org/3/library/asyncio.html"]}


class SearchEvidenceTests(unittest.TestCase):
    def evaluate(self, *urls):
        return search_smoke.evaluate(CASE, [{"url": url} for url in urls])

    def test_homepage_is_not_reference_even_when_domain_passes(self):
        result = self.evaluate("https://docs.python.org/zh-cn/")
        self.assertTrue(result["domain_check"])
        self.assertFalse(result["reference_found"])
        self.assertIsNone(result["first_reference_rank"])
        self.assertEqual(result["reciprocal_rank"], 0)

    def test_reference_rank_and_fragment_query_normalization(self):
        result = self.evaluate("https://docs.python.org/", "https://docs.python.org:443/3/library/asyncio.html?highlight=asyncio#hello")
        self.assertTrue(result["reference_found"])
        self.assertEqual(result["first_reference_rank"], 2)
        self.assertEqual(result["reciprocal_rank"], 0.5)

    def test_reference_path_and_port_are_not_loose_prefixes(self):
        result = self.evaluate("https://docs.python.org/3/library/asyncio.html.fake",
                               "https://docs.python.org:8000/3/library/asyncio.html",
                               "https://docs.python.org:0/3/library/asyncio.html")
        self.assertTrue(result["domain_check"])
        self.assertFalse(result["reference_found"])

    def test_wrong_hosts_and_invalid_urls_fail_domain_invariant(self):
        urls = ["https://docs.python.org.evil.example/3/library/asyncio.html", "javascript:alert(1)",
                "https://docs.python.org:bad/3/library/asyncio.html", "https://user@docs.python.org/",
                "\nhttps://docs.python.org/3/library/asyncio.html"]
        result = self.evaluate(*urls)
        self.assertFalse(result["domain_check"])
        self.assertEqual(result["domain_violations"], urls)
        self.assertFalse(result["reference_found"])

    def test_empty_incomplete_search_does_not_pass_domain_check(self):
        row = {**self.evaluate(), "strategy": "merge", "language": "zh", "complete": False, "elapsed_seconds": 3}
        self.assertIsNone(row["domain_check"])
        summary = search_smoke.summarize([row])[0]
        self.assertEqual(summary["reference_found"], 0)
        self.assertEqual(summary["complete_searches"], 0)
        self.assertEqual(summary["incomplete_without_reference"], 1)

    def test_summary_keeps_language_and_strategy_separate(self):
        rows = [{**self.evaluate(CASE["reference_urls"][0]), "strategy": strategy, "language": language,
                 "complete": True, "elapsed_seconds": 2} for strategy, language in (("fallback", "en"), ("merge", "zh"))]
        summary = search_smoke.summarize(rows)
        self.assertEqual(len(summary), 2)
        self.assertTrue(all(row["runs"] == 1 and row["mean_reciprocal_rank"] == 1 for row in summary))

    def test_cases_require_valid_references_and_unique_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            for cases in ([CASE, CASE], [{**CASE, "reference_urls": ["https://example.org/"]}],
                          [{**CASE, "include_domains": ["docs..python.org"]}], [], {}):
                path.write_text(json.dumps(cases), encoding="utf-8")
                with self.assertRaises(ValueError):
                    search_smoke.load_cases(path)
        default = search_smoke.load_cases(Path(search_smoke.__file__).with_name("search_cases.json"))
        self.assertEqual(len(default), 6)
        self.assertEqual({case["language"] for case in default}, {"en", "zh"})

    def test_checkpoint_preserves_previous_results_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            first = {"run_complete": False, "planned_runs": 2, "records": []}
            search_smoke.checkpoint(path, first)
            original = path.read_bytes()
            with patch.object(search_smoke.os, "replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    search_smoke.checkpoint(path, {**first, "run_complete": True})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_published_evidence_matches_current_reference_rules(self):
        path = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "search-reference-2026-10-04.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        cases = {case["id"]: case for case in report["cases"]}
        self.assertTrue(report["run_complete"])
        self.assertEqual(len(report["records"]), report["planned_runs"])
        self.assertEqual(sum(row["reference_found"] for row in report["records"]), 2)
        for row in report["records"]:
            expected = search_smoke.evaluate(cases[row["case_id"]], row["results"])
            self.assertEqual({key: row[key] for key in expected}, expected)
        self.assertEqual(report["summary"], search_smoke.summarize(report["records"]))
