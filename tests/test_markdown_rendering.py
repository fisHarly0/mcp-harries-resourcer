"""Check rendered semantics with an independent CommonMark/GFM implementation."""
from pathlib import Path
import unittest

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from mcp_harries_resourcer.page_content import extract_markdown


FIXTURES = Path(__file__).parent / "fixtures"
STRUCTURED_HTML = (FIXTURES / "structured_article.html").read_text(encoding="utf-8")
BASE_HTML = (FIXTURES / "technical_article.html").read_text(encoding="utf-8")
URL = "https://example.org/docs/guide"


def render(markdown):
    return BeautifulSoup(MarkdownIt("commonmark").enable("table").render(markdown), "html.parser")


class MarkdownRenderingTests(unittest.TestCase):
    def extract(self, extra):
        result = extract_markdown(BASE_HTML.replace("</article>", extra + "</article>"), URL)
        self.assertNotRegex(result["markdown"], r"RESOURCER(?:CODE|PRE|LIST|START)[0-9a-f]{32}")
        return render(result["markdown"])

    def test_table_code_pipes_backslashes_and_backticks_remain_in_one_cell(self):
        result = extract_markdown(STRUCTURED_HTML, URL)
        document = render(result["markdown"])
        rows = document.select("table tr")
        self.assertEqual(len(rows), 6)
        self.assertEqual([len(r.find_all(["th", "td"], recursive=False)) for r in rows], [2] * 6)
        self.assertEqual([r.find("td").get_text() for r in rows[1:]],
                         ["a | b", r"a \| b", "`left` | `right`", "x | y", ""])
        self.assertEqual([r.find_all("td")[1].get_text() for r in rows[1:]],
                         ["Union", "Literal backslash", "Quoted operands", "Alternatives", "Empty expression"])
        self.assertEqual(document.select_one("table strong").get_text(), "x | y")
        self.assertIn("`a | b`", result["markdown"])  # Outside the table: no extra escape.
        self.assertEqual(result["structure"], {"code_blocks": 1, "tables": 1, "links": 1})

    def test_ordered_list_start_nested_lists_and_parent_continuations(self):
        result = extract_markdown(STRUCTURED_HTML, URL)
        document = render(result["markdown"])
        outer = document.find("ol")
        self.assertEqual(outer.get("start"), "9")
        items = outer.find_all("li", recursive=False)
        self.assertEqual(len(items), 2)
        self.assertEqual(len(items[0].find("ul").find_all("li", recursive=False)), 2)
        self.assertEqual([li.get_text(" ", strip=True) for li in items[0].select("ul > li > ol > li")],
                         ["Check the version", "Read the runtime guide"])
        self.assertEqual(items[0].find_all("p", recursive=False)[-1].get_text(),
                         "Keep this confirmation attached to step nine.")
        self.assertEqual(items[1].find_all("p", recursive=False)[-1].get_text(),
                         "Keep this explanation attached to step ten.")
        self.assertEqual(items[1].select_one("pre > code").get_text(),
                         'def deploy():\n    for label in ["甲", "乙"]:\n        print(label)\n\ndeploy()\n')
        self.assertEqual(items[1].select_one("pre > code").get("class"), ["language-python"])
        self.assertEqual(result["references"], [{"url": "https://example.org/api/runtime", "text": "runtime guide"}])
        self.assertNotIn("Navigation only", document.get_text())
        self.assertNotIn("Footer only", document.get_text())

    def test_two_digit_width_transition_keeps_children_attached(self):
        items = "".join(f"<li>Step {i}<ul><li>Check {i}</li></ul></li>" for i in range(1, 13))
        document = self.extract("<ol>" + items + "</ol>")
        outer = document.find("ol")
        rendered = outer.find_all("li", recursive=False)
        self.assertEqual(len(rendered), 12)
        for i, item in enumerate(rendered, 1):
            self.assertEqual(item.find("ul").find("li").get_text(strip=True), f"Check {i}")

    def test_list_code_block_with_nested_backticks_retains_exact_code(self):
        code = 'def nested():\n    value = "``` | literal"\n\n    return value\n'
        document = self.extract('<ol><li>Run example<pre><code class="language-python">' + code +
                                '</code></pre><p>After example</p></li><li>Continue</li></ol>')
        items = document.find("ol").find_all("li", recursive=False)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].select_one("pre > code").get_text(), code)
        self.assertEqual(items[0].find_all("p", recursive=False)[-1].get_text(), "After example")

    def test_only_nested_list_and_mixed_levels_are_not_flattened(self):
        document = self.extract('<ol><li><ul><li>Only child<ol><li>Deep child</li></ol></li></ul></li></ol>')
        self.assertEqual(len(document.select("ol > li > ul > li > ol > li")), 1)
        self.assertEqual(document.select_one("ol > li > ul > li > ol > li").get_text(), "Deep child")

    def test_start_zero_and_bad_start_do_not_leak_markers(self):
        for value, expected in [("0", "0"), ("20", "20"), ("bad", None), ("1000000000", None)]:
            with self.subTest(start=value):
                document = self.extract(f'<ol start="{value}"><li>First step</li><li>Second step</li></ol>')
                self.assertEqual(document.find("ol").get("start"), expected)

    def test_start_marker_does_not_turn_literal_text_into_a_heading(self):
        document = self.extract('<ol start="3"><li># This is literal text</li></ol>')
        item = document.find("ol").find("li")
        self.assertEqual(item.get_text(strip=True), "# This is literal text")
        self.assertIsNone(item.find("h1"))

    def test_largest_supported_start_keeps_following_items_in_the_list(self):
        document = self.extract('<ol start="999999999"><li>First step</li>'
                                '<li>Second step<ul><li>Check second</li></ul></li></ol>')
        outer = document.find("ol")
        self.assertEqual(outer.get("start"), "999999999")
        self.assertEqual(len(outer.find_all("li", recursive=False)), 2)
        self.assertEqual(outer.find("ul").get_text(strip=True), "Check second")

    def test_adjacent_lists_keep_distinct_numbering(self):
        document = self.extract('<ol start="3"><li>First sequence</li></ol>'
                                '<ol start="7"><li>Second sequence</li></ol>')
        lists = document.find_all("ol")
        self.assertEqual([lst.get("start") for lst in lists], ["3", "7"])
        self.assertEqual([lst.get_text(strip=True) for lst in lists], ["First sequence", "Second sequence"])

    def test_nested_list_in_table_remains_one_cell(self):
        document = self.extract('<table><tr><th>Options</th><th>Purpose</th></tr>'
                                '<tr><td><ul><li>Option alpha</li><li><code>a | b</code></li></ul></td>'
                                '<td>Choose one</td></tr></table>')
        cells = document.find_all("table")[-1].find("tbody").find_all("td")
        self.assertEqual(len(cells), 2)
        self.assertIn("a | b", cells[0].get_text())
        self.assertEqual(cells[1].get_text(), "Choose one")

    def test_structured_document_is_deterministic(self):
        self.assertEqual(extract_markdown(STRUCTURED_HTML, URL), extract_markdown(STRUCTURED_HTML, URL))
