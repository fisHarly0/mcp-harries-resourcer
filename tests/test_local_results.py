from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp_harries_resourcer import local_search
from mcp_harries_resourcer.local_results import RankedFiles


class LocalResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, content, **metadata):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        head = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in metadata.items()) + "\n---\n" if metadata else ""
        path.write_text(head + content, encoding="utf-8")
        return path

    def search(self, query="needle", **kwargs):
        return local_search.search(query, str(self.root), **kwargs)

    @contextmanager
    def traversal(self, reverse=False):
        original = os.scandir
        @contextmanager
        def ordered(path):
            with original(path) as entries:
                yield iter(sorted(entries, key=lambda entry: entry.name, reverse=reverse))
        with patch.object(local_search.os, "scandir", ordered):
            yield

    def test_title_after_repeated_log_is_kept_and_legacy_line_limit_remains(self):
        noise = self.write("00-log.md", "incidental needle mention\n" * 60)
        guide = self.write("99-guide.md", "Useful topic explanation", title="Needle guide")
        with self.traversal():
            lines = self.search(max_results=3)
            files = self.search(max_results=1, result_mode="files")
        self.assertEqual([h["path"] for h in lines["matches"]], [str(noise)] * 3)
        self.assertEqual(lines["stop_reason"], "max_results")
        self.assertEqual([h["path"] for h in files["matches"]], [str(guide)])
        self.assertTrue(files["scan_complete"])
        self.assertTrue(files["results_truncated"])
        self.assertFalse(files["complete"])
        self.assertIsNone(files["stop_reason"])
        self.assertEqual(files["matched_files"], 2)
        self.assertEqual(files["stats"]["files_read"], 2)
        formatted = local_search.format_result(files)
        self.assertIn("仅展示排名前 1 个", formatted)
        self.assertNotIn("有文件未能搜索", formatted)

    def test_field_priority_and_ties_are_independent_of_traversal_order(self):
        text = self.write("a-text.md", "needle\n" * 100)
        filename = self.write("b-needle.md", "other topic")
        tag = self.write("c-tag.md", "other topic", tags=["needle"])
        title_z = self.write("z-title.md", "other", title="needle")
        title_d = self.write("d-title.md", "other", title="needle")
        expected = [title_d, title_z, tag, filename, text]
        for reverse in (False, True):
            with self.traversal(reverse):
                result = self.search(result_mode="files")
            self.assertEqual([h["path"] for h in result["matches"]], list(map(str, expected)))
            self.assertTrue(result["complete"])
            self.assertEqual(result["matches"][0]["matched_fields"], ["title", "text"])
            self.assertEqual(result["matches"][-1]["matched_lines"], 100)

    def test_filename_only_match_has_no_fabricated_line_number(self):
        path = self.write("needle.md", "unrelated")
        self.assertEqual(self.search()["matches"], [])
        hit, = self.search(result_mode="files")["matches"]
        self.assertEqual(hit["path"], str(path))
        self.assertIsNone(hit["line"])
        self.assertEqual(hit["preview_source"], "filename")
        self.assertEqual(hit["matched_lines"], 0)
        self.assertEqual(hit["text"], "needle.md")

    def test_decoded_title_and_tag_can_match_without_matching_json_escape(self):
        for field in ("title", "tags"):
            with self.subTest(field=field):
                value = 'a "needle" quote'
                path = self.write(field + ".md", "unrelated", **{field: [value] if field == "tags" else value})
                hit = next(h for h in self.search('"needle"', result_mode="files")["matches"] if h["path"] == str(path))
                self.assertIsNone(hit["line"])
                self.assertEqual(hit["preview_source"], field)
                self.assertIn('"needle"', hit["text"])

    def test_filters_apply_before_ranking_and_full_metadata_drives_rank(self):
        self.write("bad.md", "needle", title="needle", tags=["other"], source_url="https://python.org.evil.org")
        title = "x" * 500 + "needle"
        good = self.write("good.md", "body", title=title, tags=["mcp"], source_url="https://docs.python.org/3/")
        result = self.search(result_mode="files", tags="MCP", source_domain="python.org")
        hit, = result["matches"]
        self.assertEqual(hit["path"], str(good))
        self.assertEqual(hit["matched_fields"][0], "title")
        self.assertTrue(hit["metadata_truncated"])
        self.assertIn("needle", hit["text"])

    def test_limit_only_truncates_output_and_empty_filter_browsing_is_sorted(self):
        for name in ("c.md", "a.md", "b.md"):
            self.write(name, "body", tags=["mcp"])
        result = self.search("", tags="mcp", result_mode="files", max_results=2)
        self.assertEqual([Path(h["path"]).name for h in result["matches"]], ["a.md", "b.md"])
        self.assertEqual(result["matched_files"], 3)
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["complete"])
        self.assertTrue(all(h["line"] is None and h["matched_lines"] == 0 for h in result["matches"]))
        complete = self.search("", tags="mcp", result_mode="files", max_results=3)
        self.assertTrue(complete["complete"])
        self.assertFalse(complete["results_truncated"])

    def test_partial_entry_scan_never_claims_global_ranking(self):
        self.write("a.md", "needle")
        self.write("z.md", "needle", title="needle guide")
        with self.traversal():
            result = self.search(result_mode="files", max_entries=1)
        self.assertFalse(result["scan_complete"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["stop_reason"], "max_entries")
        self.assertEqual(len(result["matches"]), 1)
        self.assertIn("排序仅限已完成扫描的文件", local_search.format_result(result))

    def test_skipped_bad_file_and_excluded_tree_keep_existing_boundaries(self):
        self.write("node_modules/ignored.md", "needle", title="needle")
        self.write("good.md", "needle")
        (self.root / "bad.md").write_bytes(b"needle\xff")
        result = self.search(result_mode="files")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["skipped"]["encoding"], 1)
        self.assertEqual(result["skipped"]["excluded_dirs"], 1)
        self.assertFalse(result["scan_complete"])

    def test_updates_deletion_and_invalid_mode(self):
        path = self.write("guide.md", "needle", title="needle")
        self.assertEqual(len(self.search(result_mode="files")["matches"]), 1)
        path.write_text("different", encoding="utf-8")
        self.assertEqual(self.search(result_mode="files")["matches"], [])
        path.unlink()
        self.assertEqual(self.search(result_mode="files")["matches"], [])
        with self.assertRaisesRegex(ValueError, "result_mode"):
            self.search(result_mode="unknown")

    def test_cancel_after_first_file_preserves_only_completed_files(self):
        self.write("a.md", "needle")
        self.write("z.md", "needle", title="needle")
        cancelled = threading.Event()
        original = os.scandir
        @contextmanager
        def gated(path):
            with original(path) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
                def iterator():
                    yield ordered[0]
                    cancelled.set()
                    yield ordered[1]
                yield iterator()
        with patch.object(local_search.os, "scandir", gated):
            result = self.search(result_mode="files", cancelled=cancelled)
        self.assertEqual(result["stop_reason"], "cancelled")
        self.assertEqual(result["matched_files"], 1)
        self.assertFalse(result["scan_complete"])

    def test_deadline_after_first_file_does_not_discard_its_result(self):
        self.write("a.md", "needle")
        self.write("z.md", "needle", title="needle")
        clock = [100.0]
        original = os.scandir
        @contextmanager
        def gated(path):
            with original(path) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
                def iterator():
                    yield ordered[0]
                    clock[0] += 1
                    yield ordered[1]
                yield iterator()
        with patch.object(local_search.os, "scandir", gated), patch.object(
                local_search.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.search(result_mode="files", time_budget_seconds=0.5)
        self.assertEqual(result["stop_reason"], "time_budget")
        self.assertEqual(result["matched_files"], 1)
        self.assertEqual(len(result["matches"]), 1)
        self.assertFalse(result["scan_complete"])

    def test_long_line_preview_contains_match_and_reports_original_column(self):
        line = "prefix " * 60 + "needle relevant explanation"
        self.write("long.md", line)
        hit, = self.search()["matches"]
        self.assertIn("needle", hit["text"])
        self.assertEqual(hit["column"], line.index("needle") + 1)
        self.assertEqual(hit["text"], line[hit["text_start"]:hit["text_end"]])
        self.assertLessEqual(len(hit["text"]), 200)
        self.assertTrue(hit["text_truncated"])

    def test_unicode_casefold_expansions_before_and_inside_match_keep_columns(self):
        for prefix, word, query in (("ß" * 5000, "NEEDLE", "needle"),
                ("prefix " * 70, "Straße", "STRASSE"), ("İ" * 5000, "中文资料", "资料")):
            with self.subTest(query=query):
                line = prefix + word
                self.write("unicode.md", line)
                hit, = self.search(query)["matches"]
                expected = len(prefix) + (2 if query == "资料" else 0)
                self.assertEqual(hit["column"], expected + 1)
                self.assertIn(query.casefold(), hit["text"].casefold())
                self.assertEqual(hit["text"], line[hit["text_start"]:hit["text_end"]])

    def test_short_line_whitespace_and_long_query_preview_are_literal(self):
        self.write("spaces.md", "  needle  ")
        hit, = self.search(" needle ")["matches"]
        self.assertEqual(hit["text"], "  needle  ")
        self.assertEqual(hit["column"], 2)
        long_query = "needle" * 60
        self.write("long.md", "prefix " * 60 + long_query)
        hit, = self.search(long_query)["matches"]
        self.assertTrue(hit["text"].startswith("needle"))
        self.assertEqual(len(hit["text"]), 200)
        self.assertTrue(hit["text_truncated"])

    def test_text_output_marks_only_sides_actually_truncated(self):
        line = "x" * 200 + "needle" + "y" * 10
        self.write("one.md", line)
        result = self.search()
        text = local_search.format_result(result)
        preview_line = next(line for line in text.splitlines() if "L1:" in line)
        self.assertIn("…", preview_line)
        self.assertFalse(preview_line.endswith("…"))

    def test_heap_keeps_only_limit_and_selects_best_seen_in_any_order(self):
        pool = RankedFiles(3)
        for i in range(1000, -1, -1):
            pool.add({"path": str(i)}, i % 5, f"{i:04d}")
            self.assertLessEqual(len(pool.heap), 3)
        self.assertEqual(pool.count, 1001)
        self.assertEqual([h["path"] for h in pool.results()], ["4", "9", "14"])
