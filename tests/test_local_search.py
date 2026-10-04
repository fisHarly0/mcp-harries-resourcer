import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mcp_harries_resourcer import local_search
from mcp_harries_resourcer import server


class LocalSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def search(self, query="needle", **kwargs):
        return local_search.search(query, str(self.root), **kwargs)

    def note(self, title="标题", **kwargs):
        with patch.object(server, "RESEARCH_ROOT", self.root):
            server.save_finding("Python / 笔记", title, "needle 正文\n第二行 Needle", **kwargs)

    def test_saved_metadata_filters_and_browsing(self):
        self.note(tags="asyncio, MCP", source_url="https://docs.python.org/3/")
        self.note("干扰", tags="asyncio", source_url="https://python.org.evil.example/")
        found = self.search(collection="python / 笔记", tags="MCP, ASYNCIO", source_domain="PYTHON.ORG")
        self.assertTrue(found["complete"])
        self.assertEqual(len(found["matches"]), 2)
        self.assertEqual(found["matches"][0]["metadata"]["title"], "标题")
        self.assertEqual(found["stats"]["filtered_files"], 1)
        browsing = self.search("", tags="mcp")
        self.assertEqual(len(browsing["matches"]), 1)
        self.assertIsNone(browsing["matches"][0]["line"])
        self.assertEqual(self.search("", source_domain="python.org")["stats"]["filtered_files"], 1)

    def test_updates_and_deletes_are_visible(self):
        path = self.write("note.md", "needle")
        self.assertEqual(len(self.search()["matches"]), 1)
        path.write_text("changed", encoding="utf-8")
        self.assertEqual(self.search()["matches"], [])
        self.assertEqual(len(self.search("changed")["matches"]), 1)
        path.unlink()
        self.assertEqual(self.search("changed")["matches"], [])

    def test_excluded_subtrees_never_opened_and_root_not_excluded(self):
        self.write("node_modules/nested/ignored.md", "needle")
        self.write("notes/good.md", "needle")
        scandir = os.scandir
        visited = []
        def tracked(path):
            visited.append(Path(path))
            return scandir(path)
        with patch.object(local_search.os, "scandir", tracked):
            found = self.search()
        self.assertEqual(len(found["matches"]), 1)
        self.assertNotIn(self.root / "node_modules", visited)
        self.assertEqual(found["skipped"]["excluded_dirs"], 1)
        direct = local_search.search("needle", str(self.root / "node_modules"))
        self.assertEqual(len(direct["matches"]), 1)
        self.assertEqual(len(self.search(exclude_dirs="")["matches"]), 2)

    def test_custom_exclusion_and_extensions(self):
        self.write("private/a.md", "needle")
        self.write("note.MD", "needle")
        self.write("code.py", "needle")
        found = self.search(exclude_dirs="PRIVATE", include_ext=".md")
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(found["skipped"]["extensions"], 1)

    def test_bounded_reads_invalid_encoding_and_binary_are_reported(self):
        self.write("large.md", "needle" * 100)
        (self.root / "bad.md").write_bytes(b"needle\xff")
        (self.root / "binary.md").write_bytes(b"needle\0")
        (self.root / "bom.md").write_bytes(b"\xef\xbb\xbfneedle\r\n")
        found = self.search(max_file_bytes=64)
        self.assertFalse(found["complete"])
        self.assertEqual(len(found["matches"]), 1)
        for name in ("oversized", "encoding", "binary"):
            self.assertEqual(found["skipped"][name], 1)
        self.assertIn("搜索不完整", local_search.format_result(found))

    def test_invalid_frontmatter_is_not_used_for_filters(self):
        self.write("bad.md", '---\ntags: ["mcp"]\ncollection: unsafe plain YAML\n---\nneedle')
        self.assertEqual(len(self.search()["matches"]), 1)
        found = self.search("", tags="mcp")
        self.assertEqual(found["matches"], [])
        self.assertFalse(found["complete"])
        self.assertEqual(found["skipped"]["metadata"], 1)

    def test_file_growth_does_not_return_partial_matches(self):
        self.write("growing.md", "needle")
        with patch.object(Path, "open", return_value=io.BytesIO(b"needle" + b"x" * 100)):
            found = self.search(max_file_bytes=64)
        self.assertEqual(found["matches"], [])
        self.assertEqual(found["skipped"]["oversized"], 1)
        self.assertFalse(found["complete"])

    def test_line_numbers_and_unicode(self):
        self.write("unicode.md", "first\nStraße 中文\nlast")
        found = self.search("STRASSE")
        self.assertEqual(found["matches"][0]["line"], 2)
        self.assertEqual(found["matches"][0]["text"], "Straße 中文")

    def test_metadata_output_is_bounded_without_changing_filters(self):
        title = "x" * 2000
        self.note(title, tags="mcp," + "z" * 200)
        found = self.search("", tags="z" * 200)
        hit = found["matches"][0]
        self.assertEqual(len(hit["metadata"]["title"]), 256)
        self.assertTrue(hit["metadata_truncated"])
        self.assertLess(len(json.dumps(hit)), 2000)

    def test_result_and_entry_limits_are_explicit(self):
        for i in range(8):
            self.write(f"{i}.md", "needle\nneedle")
        found = self.search(max_results=1)
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(found["stop_reason"], "max_results")
        self.assertFalse(found["complete"])
        found = self.search("absent", max_entries=3)
        self.assertEqual(found["stats"]["entries"], 3)
        self.assertEqual(found["stop_reason"], "max_entries")
        self.assertFalse(found["complete"])

    def test_deadline_and_io_error_preserve_diagnostics(self):
        self.write("good.md", "needle")
        real = os.scandir
        def slow(path):
            time.sleep(0.02)
            return real(path)
        with patch.object(local_search.os, "scandir", slow):
            found = self.search(time_budget_seconds=0.01)
        self.assertEqual(found["stop_reason"], "time_budget")
        with patch.object(local_search.os, "scandir", side_effect=PermissionError):
            found = self.search()
        self.assertEqual(found["skipped"]["io_errors"], 1)
        self.assertFalse(found["complete"])

    def test_invalid_arguments_and_root(self):
        for kwargs in ({"max_results": 0}, {"max_results": -1}, {"max_entries": 0},
                       {"max_file_bytes": 0}, {"time_budget_seconds": float("nan")},
                       {"time_budget_seconds": 0}, {"exclude_dirs": "a/b"},
                       {"exclude_dirs": "a*"}, {"source_domain": "https://example.org"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.search(**kwargs)
        with self.assertRaisesRegex(ValueError, "关键词不能为空"):
            self.search(" ")
        self.write("file", "needle")
        for path in (self.root / "missing", self.root / "file"):
            with self.assertRaises(ValueError):
                local_search.search("needle", str(path))

    def test_symlinks_are_not_followed(self):
        target = self.write("actual/note.md", "needle")
        try:
            (self.root / "linked.md").symlink_to(target)
            (self.root / "actual" / "cycle").symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink privilege unavailable: {type(exc).__name__}")
        found = self.search()
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(found["skipped"]["links"], 2)

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_windows_junction_is_not_followed(self):
        import subprocess
        self.write("target/note.md", "needle")
        junction = self.root / "junction"
        # Native PowerShell only; the link and target both stay in this temp root.
        quoted_link = str(junction).replace("'", "''")
        quoted_target = str(self.root / "target").replace("'", "''")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"New-Item -ItemType Junction -Path '{quoted_link}' -Target '{quoted_target}' | Out-Null"],
                       check=True, capture_output=True)
        try:
            found = self.search()
            self.assertEqual(len(found["matches"]), 1)
            self.assertEqual(found["skipped"]["links"], 1)
        finally:
            os.rmdir(junction)  # Remove only the junction, never recurse through it.


class LocalAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_keeps_loop_responsive_and_waits_for_worker(self):
        for mode in ("lines", "files"):
            with self.subTest(mode=mode):
                await self._check_cancellation(mode)

    async def _check_cancellation(self, mode):
        started, release = threading.Event(), threading.Event()
        real = os.scandir
        def gated(path):
            started.set()
            release.wait(3)
            return real(path)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "note.md").write_text("needle", encoding="utf-8")
            with patch.object(local_search.os, "scandir", gated), patch.object(Path, "open", side_effect=AssertionError("cancelled scan read a file")) as opened:
                task = asyncio.create_task(local_search.search_async("needle", tmp, result_mode=mode))
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                opened.assert_not_called()

    async def test_tool_validation_returns_json(self):
        result = json.loads(await server.search_local("", ".", response_format="json"))
        self.assertFalse(result["ok"])
        self.assertIn("关键词不能为空", result["error"])
