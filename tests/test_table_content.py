"""Verify table cell positions after extraction and independent GFM rendering."""
from pathlib import Path
import unittest

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from mcp_harries_resourcer.page_content import extract_markdown


BASE = (Path(__file__).parent / "fixtures" / "technical_article.html").read_text(encoding="utf-8")
URL = "https://example.org/docs/guide"


class TableContentTests(unittest.TestCase):
    def extract(self, table):
        result = extract_markdown(BASE.replace("</article>", table + "</article>"), URL)
        document = BeautifulSoup(MarkdownIt().enable("table").render(result["markdown"]), "html.parser")
        return result, document

    def matrix(self, document):
        table = document.find_all("table")[-1]
        return [[c.get_text(" ", strip=True) for c in r.find_all(["td", "th"], recursive=False)]
                for r in table.find_all("tr", recursive=False) + table.select("thead > tr, tbody > tr")]

    def test_rowspan_zero_stops_at_its_own_row_group(self):
        _, document = self.extract('<table><thead><tr><th>Group</th><th>Value</th></tr></thead>'
            '<tbody><tr><td rowspan="0">A</td><td>one</td></tr><tr><td>two</td></tr></tbody>'
            '<tbody><tr><td>B</td><td>three</td></tr></tbody></table>')
        self.assertEqual(self.matrix(document), [["Group", "Value"], ["A", "one"], ["", "two"], ["B", "three"]])

    def test_positive_rowspan_cannot_push_next_group_to_the_right(self):
        _, document = self.extract('<table><thead><tr><th rowspan="2">Group</th><th>Value</th></tr></thead>'
                                   '<tbody><tr><td>A</td><td>one</td></tr></tbody></table>')
        self.assertEqual(self.matrix(document), [["Group", "Value"], ["A", "one"]])

    def test_caption_is_not_a_header_or_a_spurious_data_row(self):
        result, document = self.extract('<table><caption>Read <a href="../source">source settings</a></caption>'
            '<thead><tr><th>Group</th><th>Value</th></tr></thead><tbody><tr><td>A</td><td>one</td></tr></tbody></table>')
        self.assertEqual(self.matrix(document), [["Group", "Value"], ["A", "one"]])
        caption = document.find_all("table")[-1].find_previous_sibling("p")
        self.assertEqual(caption.get_text(), "Read source settings")
        self.assertEqual(caption.a["href"], "https://example.org/source")
        self.assertIn({"url": "https://example.org/source", "text": "source settings"}, result["references"])

    def test_headerless_table_gets_an_empty_header_and_retains_all_data_rows(self):
        _, document = self.extract('<table><tr><td>Alpha</td><td>10</td></tr><tr><td>Beta</td><td>20</td></tr></table>')
        self.assertEqual(self.matrix(document), [["", ""], ["Alpha", "10"], ["Beta", "20"]])

    def test_nonfirst_header_does_not_turn_earlier_data_into_plain_text(self):
        _, document = self.extract('<table><tr><td>First record</td><td>10</td></tr>'
            '<tr><th>Section name</th><th>Amount</th></tr><tr><td>Second record</td><td>20</td></tr></table>')
        self.assertEqual(self.matrix(document), [["", ""], ["First record", "10"],
                                                ["Section name", "Amount"], ["Second record", "20"]])

    def test_combined_rowspan_and_colspan_keep_multilevel_header_alignment(self):
        _, document = self.extract('<table><thead><tr><th rowspan="2">Product</th><th colspan="2">Limits</th></tr>'
            '<tr><th>Minimum</th><th>Maximum</th></tr></thead><tbody><tr><td rowspan="2">Python</td>'
            '<td>3.10</td><td>3.12</td></tr><tr><td>3.13</td><td>3.14</td></tr></tbody></table>')
        self.assertEqual(self.matrix(document), [["Product", "Limits", ""], ["", "Minimum", "Maximum"],
                                                ["Python", "3.10", "3.12"], ["", "3.13", "3.14"]])

    def test_zero_colspan_still_occupies_one_column(self):
        _, document = self.extract('<table><tr><th>Group</th><th>Value</th></tr>'
            '<tr><td colspan="0" rowspan="2">A</td><td>one</td></tr><tr><td>two</td></tr></table>')
        self.assertEqual(self.matrix(document), [["Group", "Value"], ["A", "one"], ["", "two"]])

    def test_implicit_row_group_supports_rowspan_zero(self):
        _, document = self.extract('<table><tr><th>Group</th><th>Value</th></tr>'
            '<tr><td rowspan="0">A</td><td>one</td></tr><tr><td>two</td></tr></table>')
        self.assertEqual(self.matrix(document), [["Group", "Value"], ["A", "one"], ["", "two"]])

    def test_short_rows_are_padded_without_dropping_longer_rows(self):
        _, document = self.extract('<table><tr><th>Item</th></tr><tr><td>A</td><td>one</td><td>extra</td></tr>'
                                   '<tr><td>B</td><td>two</td></tr></table>')
        self.assertEqual(self.matrix(document), [["Item", "", ""], ["A", "one", "extra"], ["B", "two", ""]])

    def test_huge_or_invalid_rowspan_is_bounded_by_existing_rows(self):
        for value in ["9" * 5000, "2", "+2", " 2 ", "bad", "-1"]:
            with self.subTest(value=value[:10]):
                _, document = self.extract('<table><thead><tr><th rowspan="' + value + '">Item</th>'
                    '<th>Value</th></tr></thead><tbody><tr><td>A</td><td>one</td></tr></tbody></table>')
                self.assertEqual(self.matrix(document), [["Item", "Value"], ["A", "one"]])

    def test_table_in_list_keeps_pipe_code_and_column_positions(self):
        result, document = self.extract('<ol><li>Read options<table><tr><td><code>a | b</code></td><td>Union</td></tr>'
                                       '<tr><td>plain</td><td>Text</td></tr></table></li></ol>')
        table = document.find("ol").find("table")
        self.assertIsNotNone(table)
        self.assertEqual([cell.get_text() for cell in table.select("tbody tr")[0].find_all("td")], ["a | b", "Union"])
        self.assertNotIn("RESOURCER", result["markdown"])

    def test_multiple_tables_keep_their_item_and_surrounding_text(self):
        result, document = self.extract('<ol><li>Before first<table><tr><td>Alpha</td><td>10</td></tr></table>'
            '<p>Between tables</p><table><tr><td>Beta</td><td>20</td></tr></table><p>After second</p></li>'
            '<li>Other item<table><tr><td>Gamma</td><td>30</td></tr></table><p>Final text</p></li></ol>')
        items = document.find("ol").find_all("li", recursive=False)
        self.assertEqual(len(items), 2)
        self.assertEqual([t.select_one("tbody td").get_text() for t in items[0].find_all("table")], ["Alpha", "Beta"])
        self.assertEqual(items[0].get_text(" ", strip=True), "Before first Alpha 10 Between tables Beta 20 After second")
        self.assertEqual(items[1].get_text(" ", strip=True), "Other item Gamma 30 Final text")
        self.assertNotIn("RESOURCER", result["markdown"])

    def test_definition_table_caption_links_and_text_survive(self):
        result, document = self.extract('<dl><dt>Configuration</dt><dd>Read details<table>'
            '<caption>Shared <a href="/source">source</a></caption><tr><th>Key</th><th>Value</th></tr>'
            '<tr><td>Limit</td><td>10</td></tr></table><p>After settings</p></dd></dl>')
        self.assertEqual(self.matrix(document), [["Key", "Value"], ["Limit", "10"]])
        text = document.get_text(" ", strip=True)
        self.assertLess(text.index("Shared source"), text.index("Key Value"))
        self.assertLess(text.index("Limit 10"), text.index("After settings"))
        self.assertIn({"url": "https://example.org/source", "text": "source"}, result["references"])
        self.assertNotIn("RESOURCER", result["markdown"])

    def test_nested_table_restoration_is_deterministic(self):
        source = '<ol><li>Settings<table><tr><td>Alpha</td><td>10</td></tr></table></li></ol>'
        self.assertEqual(self.extract(source)[0], self.extract(source)[0])
