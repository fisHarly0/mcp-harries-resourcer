from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp_harries_resourcer import local_search
from mcp_harries_resourcer.local_terms import TermQuery


class LocalTermsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, content, **meta):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        head = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) + "\n---\n" if meta else ""
        path.write_text(head + content, encoding="utf-8")
        return path

    def search(self, query="asyncio 取消", **kwargs):
        return local_search.search(query, str(self.root), **{"query_mode": "all", "result_mode": "files", **kwargs})

    def test_terms_cross_title_and_body_but_literal_default_is_unchanged(self):
        note = self.write("guide.md", "任务取消与超时", title="asyncio 指南")
        self.write("noise.md", "asyncio only")
        self.assertEqual(local_search.search("asyncio 取消", str(self.root), result_mode="files")["matches"], [])
        for query in ("asyncio 取消", "取消 asyncio"):
            hit, = self.search(query)["matches"]
            self.assertEqual(hit["path"], str(note))
            self.assertEqual(hit["matched_terms"], query.split())
            self.assertEqual(hit["rank_field"], "text")
            self.assertEqual({e["line"] for e in hit["term_matches"]}, {2, 4})
            rendered = local_search.format_result(self.search(query))
            self.assertIn("[asyncio] L2:", rendered)
            self.assertIn("[取消] L4:", rendered)

    def test_all_lines_requires_same_line_and_any_lines_retains_both(self):
        self.write("guide.txt", "asyncio\n取消\n取消 works with asyncio\n")
        all_result = self.search(result_mode="lines")
        self.assertEqual([h["line"] for h in all_result["matches"]], [3])
        self.assertEqual(all_result["matches"][0]["column"], 1)
        any_result = self.search(result_mode="lines", query_mode="any")
        self.assertEqual([h["line"] for h in any_result["matches"]], [1, 2, 3])
        self.assertEqual(any_result["matches"][0]["matched_terms"], ["asyncio"])
        self.assertTrue(any_result["complete"])

    def test_any_files_reports_only_present_terms_and_single_word_is_compatible(self):
        self.write("one.txt", "asyncio\n" * 20)
        self.write("two.txt", "取消")
        self.write("none.txt", "unrelated")
        result = self.search(query_mode="any")
        self.assertEqual(result["matched_files"], 2)
        self.assertEqual([h["matched_terms"] for h in result["matches"]], [["asyncio"], ["取消"]])
        for mode in ("all", "any"):
            hit, = self.search("ASYNCIO", query_mode=mode)["matches"]
            self.assertEqual(hit["matched_lines"], 20)

    def test_dedup_unicode_and_whitespace_preserve_first_spelling(self):
        self.write("guide.md", "Straße 和中文资料")
        result = self.search("  STRASSE\tStraße\n资料\u3000资料  ")
        self.assertEqual(result["query_terms"], ["STRASSE", "资料"])
        hit, = result["matches"]
        self.assertEqual(len(hit["term_matches"]), 2)
        self.assertEqual([e["column"] for e in hit["term_matches"]], [1, 11])

    def test_code_punctuation_and_quotes_are_literal_not_operators(self):
        self.write("code.md", 'C++\n--flag\nfoo.bar()\n"quoted"\nC:\\work\n')
        self.write("partial.md", "C foo bar quoted work")
        hit, = self.search('C++ --flag foo.bar() "quoted" C:\\work')["matches"]
        self.assertEqual(Path(hit["path"]).name, "code.md")
        self.assertEqual(len(hit["term_matches"]), 5)

    def test_substrings_work_without_english_word_boundaries_or_chinese_segmentation(self):
        self.write("note.md", "异步任务取消。处理超时。\ncataclysm")
        self.assertEqual(len(self.search("取消 超时")["matches"]), 1)
        self.assertEqual(len(self.search("cat 超时")["matches"]), 1)
        self.assertEqual(self.search("取消超时")["matches"], [])

    def test_all_ranking_requires_all_terms_at_tier_and_is_traversal_independent(self):
        self.write("a-partial.md", "取消", title="asyncio")
        self.write("b-tags.md", "body", tags=["asyncio", "取消"])
        self.write("c-title.md", "body", title="asyncio 与取消")
        self.write("d-title.md", "body", title="取消 asyncio")
        original = os.scandir
        for reverse in (False, True):
            @contextmanager
            def ordered(path):
                with original(path) as entries:
                    yield iter(sorted(entries, key=lambda entry: entry.name, reverse=reverse))
            with patch.object(local_search.os, "scandir", ordered):
                result = self.search(max_results=3)
            self.assertEqual([Path(h["path"]).name for h in result["matches"]], ["c-title.md", "d-title.md", "b-tags.md"])
            self.assertEqual([h["rank_field"] for h in result["matches"]], ["title", "title", "tags"])
            self.assertTrue(result["scan_complete"])
            self.assertTrue(result["results_truncated"])
            self.assertEqual(result["matched_files"], 4)

    def test_filename_and_decoded_tags_supply_missing_terms_without_fake_lines(self):
        self.write("asyncio.md", "body", tags=['quote"here'])
        hit, = self.search('asyncio quote"here')["matches"]
        self.assertEqual(hit["matched_fields"], ["tags", "filename"])
        self.assertEqual(hit["rank_field"], "filename")
        self.assertTrue(all(e["line"] is None for e in hit["term_matches"]))
        self.assertEqual([e["preview_source"] for e in hit["term_matches"]], ["filename", "tags"])
        self.assertEqual(hit["matched_lines"], 0)

    def test_directory_names_never_supply_a_term(self):
        self.write("asyncio/note.md", "取消")
        self.assertEqual(self.search()["matches"], [])

    def test_any_priority_uses_best_field_and_evidence_is_bounded_per_term(self):
        self.write("a-many.md", "asyncio 取消\n" * 1000)
        self.write("z-title.md", "body", title="asyncio")
        result = self.search(query_mode="any")
        self.assertEqual([Path(h["path"]).name for h in result["matches"]], ["z-title.md", "a-many.md"])
        self.assertEqual(len(result["matches"][1]["term_matches"]), 2)
        self.assertEqual(result["matches"][1]["matched_lines"], 1000)

    def test_full_title_and_multiple_tags_are_used_before_preview_truncation(self):
        self.write("title.md", "body", title="x" * 500 + "asyncio" + "y" * 500 + "取消")
        self.write("tags.md", "body", title="asyncio", tags=["other"] * 40 + ["取消"])
        result = self.search()
        self.assertEqual([h["rank_field"] for h in result["matches"]], ["title", "tags"])
        self.assertTrue(all(h["metadata_truncated"] for h in result["matches"]))
        self.assertTrue(all(e["term"] in e["text"] for h in result["matches"] for e in h["term_matches"]))

    def test_long_separated_terms_each_have_original_context_and_columns(self):
        line = "ß" * 4500 + "asyncio" + "x" * 500 + "取消"
        self.write("long.txt", line)
        for result_mode in ("lines", "files"):
            hit, = self.search(result_mode=result_mode)["matches"]
            for evidence in hit["term_matches"]:
                self.assertIn(evidence["term"], evidence["text"])
                self.assertEqual(evidence["column"], line.index(evidence["term"]) + 1)
                self.assertEqual(evidence["text"], line[evidence["text_start"]:evidence["text_end"]])
                self.assertLessEqual(len(evidence["text"]), 200)

    def test_filters_and_invalid_utf8_still_apply(self):
        self.write("valid.md", "asyncio 和取消", tags=["MCP"], source_url="https://docs.python.org/3/")
        self.write("wrong.md", "asyncio 取消", tags=["other"])
        (self.root / "bad.md").write_bytes(b"asyncio \xff")
        result = self.search(tags="MCP", source_domain="python.org")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["skipped"]["encoding"], 1)
        self.assertFalse(result["scan_complete"])

    def test_empty_query_with_filter_and_default_literal_whitespace(self):
        self.write("note.md", "  asyncio  ", tags=["MCP"])
        result = self.search(" \t", tags="MCP")
        self.assertEqual(result["query_terms"], [])
        self.assertIsNone(result["matches"][0]["line"])
        literal = self.search(" asyncio ", query_mode="literal", result_mode="lines")
        self.assertEqual(literal["matches"][0]["column"], 2)
        self.assertNotIn("query_terms", literal)

    def test_invalid_mode_and_query_bounds_fail_before_filesystem_access(self):
        for query, extra, message in (("asyncio", {"query_mode": "regex"}, "query_mode"),
                                     ("x" * 4097, {}, "4096"),
                                     (" ".join(f"term{i}" for i in range(17)), {}, "16")):
            with self.subTest(message=message), patch.object(Path, "exists", side_effect=AssertionError("touched root")):
                with self.assertRaisesRegex(ValueError, message):
                    self.search(query, **extra)
        self.write("terms.txt", " ".join(f"term{i}" for i in range(16)))
        self.assertEqual(len(self.search(" ".join(f"term{i}" for i in range(16)))["matches"]), 1)
        self.assertEqual(len(TermQuery("needle " * 100, "all").terms), 1)

    def test_line_limit_and_file_output_limit_have_distinct_completeness(self):
        self.write("a.md", "asyncio 取消\n" * 10)
        self.write("b.md", "asyncio 取消")
        lines = self.search(max_results=1, result_mode="lines")
        self.assertEqual(lines["stop_reason"], "max_results")
        files = self.search(max_results=1)
        self.assertTrue(files["scan_complete"])
        self.assertIsNone(files["stop_reason"])
        self.assertTrue(files["results_truncated"])

    def test_updates_and_deletion_are_visible_without_an_index(self):
        note = self.write("note.md", "asyncio 取消")
        self.assertEqual(len(self.search()["matches"]), 1)
        note.write_text("asyncio", encoding="utf-8")
        self.assertEqual(self.search()["matches"], [])
        note.unlink()
        self.assertEqual(self.search(query_mode="any")["matches"], [])

    def test_budget_during_a_file_discards_it_and_retains_finished_files(self):
        self.write("a.md", "asyncio 取消")
        self.write("b.md", "asyncio 取消")
        original = TermQuery.positions
        original_scandir = os.scandir
        @contextmanager
        def ordered(path):
            with original_scandir(path) as entries:
                yield iter(sorted(entries, key=lambda entry: entry.name))
        for cancellation in (False, True):
            clock, cancelled = [100.0], threading.Event()
            def gate(query, value, expired):
                if value == "b.md":
                    if cancellation:
                        cancelled.set()
                    else:
                        clock[0] += 2
                return original(query, value, expired)
            with patch.object(TermQuery, "positions", gate), patch.object(local_search.os, "scandir", ordered), patch.object(
                    local_search.time, "monotonic", side_effect=lambda: clock[0]):
                result = self.search(time_budget_seconds=1, cancelled=cancelled)
            self.assertEqual([Path(h["path"]).name for h in result["matches"]], ["a.md"])
            self.assertFalse(result["scan_complete"])
            self.assertEqual(result["stop_reason"], "cancelled" if cancellation else "time_budget")
