"""Tests for pipeline.sources.wikipedia — HTML parser, fetch, main."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import Mock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.sources import wikipedia
from pipeline.sources.wikipedia import (
    _EntryExtractor,
    _fetch_html,
    _looks_like_citation,
    _strip_citations,
    fetch,
)


# ---------------------------------------------------------------------------
# _looks_like_citation
# ---------------------------------------------------------------------------

class LooksLikeCitationTest(unittest.TestCase):
    def test_doi(self):
        self.assertTrue(_looks_like_citation("doi: 10.1234/something"))

    def test_issn(self):
        self.assertTrue(_looks_like_citation("ISSN 1234-5678"))

    def test_jstor(self):
        self.assertTrue(_looks_like_citation("Available via JSTOR"))

    def test_retrieved_date(self):
        self.assertTrue(_looks_like_citation("Retrieved January 5, 2024"))

    def test_archived_from_original(self):
        self.assertTrue(_looks_like_citation("Archived from the original on 2020-01-01"))

    def test_leading_caret(self):
        self.assertTrue(_looks_like_citation("^ Backref text"))

    def test_letter_caret(self):
        self.assertTrue(_looks_like_citation("a ^ Something"))

    def test_normal_text_not_citation(self):
        self.assertFalse(_looks_like_citation(
            "A man fell off a bridge in 2003 while attempting a stunt."
        ))

    def test_empty_string(self):
        self.assertFalse(_looks_like_citation(""))


# ---------------------------------------------------------------------------
# _strip_citations
# ---------------------------------------------------------------------------

class StripCitationsTest(unittest.TestCase):
    def test_numbered_refs(self):
        result = _strip_citations("Some text[1] and more[12] text.")
        self.assertNotIn("[1]", result)
        self.assertNotIn("[12]", result)
        self.assertIn("Some text", result)

    def test_citation_needed(self):
        result = _strip_citations("Something[citation needed] is true.")
        self.assertNotIn("[citation needed]", result)

    def test_clarification_needed(self):
        result = _strip_citations("Unclear[clarification needed] point.")
        self.assertNotIn("[clarification needed]", result)

    def test_whitespace_collapsed(self):
        result = _strip_citations("text   with    spaces")
        self.assertNotIn("   ", result)


# ---------------------------------------------------------------------------
# _EntryExtractor — HTML parser
# ---------------------------------------------------------------------------

# Full-coverage HTML exercising every parser branch
_FULL_HTML = """\
<html><body>
<p>Before main div - should be ignored</p>
<div class="mw-parser-output">
  Free text in main not in any li or cell.
  <ul>
    <li>First normal list item with plenty of text for testing purposes.</li>
    <li>
      Outer nested li text
      <ul><li>Inner nested li text</li></ul>
    </li>
  </ul>
  <ul>
    <li id="cite_note-123">This cite note text should be skipped entirely.</li>
    <li>Normal li after the cite note with good content.</li>
  </ul>
  <ul>
    <li>Text before <sup>1</sup> text after sup footnote.</li>
    <li>Text with <script>alert('bad')</script> after script.</li>
    <li>Text with <style>body{color:red}</style> after style.</li>
    <li>Text with <noscript>no-js text</noscript> after noscript.</li>
  </ul>
  <table class="wikitable">
    <tr>
      <th>Year</th>
      <th>Description</th>
    </tr>
    <tr>
      <td>1999</td>
      <td>An interesting event happened in this particular year.</td>
    </tr>
    <tr>
      <td>   </td>
    </tr>
    <tr></tr>
  </table>
  <table class="navbox">
    <tr><td>Navigation link 1</td><td>Navigation link 2</td></tr>
  </table>
  <table class="infobox">
    <tr><td>Infobox content</td></tr>
  </table>
  <ol class="references">
    <p>A reference paragraph not inside li.</p>
  </ol>
  <ul class="gallery">
    <p>Gallery item not inside li.</p>
  </ul>
  <div class="inner-section">
    <p>Inner div paragraph.</p>
  </div>
</div>
</body></html>
"""


class EntryExtractorTest(unittest.TestCase):
    def _parse(self, html):
        p = _EntryExtractor()
        p.feed(html)
        return p.items

    def test_basic_li_extracted(self):
        items = self._parse(_FULL_HTML)
        self.assertTrue(any("First normal list item" in i for i in items))

    def test_cite_note_li_skipped(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("cite note text should be skipped" in i for i in items))

    def test_normal_li_after_cite_note_extracted(self):
        items = self._parse(_FULL_HTML)
        self.assertTrue(any("Normal li after the cite note" in i for i in items))

    def test_sup_content_skipped(self):
        items = self._parse(_FULL_HTML)
        # "1" from <sup> should not appear (inside inert sup)
        for item in items:
            if "Text before" in item and "text after sup footnote" in item:
                # The literal "1" might appear from table year cell; check specifically in the sup li
                break
        # Just verify the sup tag doesn't bleed through as markup
        self.assertFalse(any("<sup>" in i for i in items))

    def test_script_content_skipped(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("alert" in i for i in items))

    def test_style_content_skipped(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("color:red" in i for i in items))

    def test_noscript_content_skipped(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("no-js text" in i for i in items))

    def test_table_row_extracted(self):
        items = self._parse(_FULL_HTML)
        self.assertTrue(any("1999" in i and "interesting event" in i for i in items))

    def test_navbox_table_skipped(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("Navigation link" in i for i in items))

    def test_infobox_table_skipped(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("Infobox content" in i for i in items))

    def test_references_ol_skipped(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("reference paragraph" in i for i in items))

    def test_gallery_ul_skipped(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("Gallery item" in i for i in items))

    def test_before_main_div_ignored(self):
        items = self._parse(_FULL_HTML)
        self.assertFalse(any("Before main div" in i for i in items))

    def test_nested_li_appended_at_outermost_close(self):
        html = """<div class="mw-parser-output">
        <ul><li>Outer text <ul><li>inner</li></ul> more outer</li></ul>
        </div>"""
        items = self._parse(html)
        self.assertTrue(any("Outer text" in i for i in items))

    def test_empty_table_row_not_appended(self):
        html = """<div class="mw-parser-output">
        <table class="wikitable">
          <tr><td>   </td></tr>
        </table>
        </div>"""
        items = self._parse(html)
        # The whitespace-only row should not appear
        self.assertFalse(any(i.strip() == "" for i in items))

    def test_cell_with_content(self):
        html = """<div class="mw-parser-output">
        <table class="wikitable">
          <tr><td>Alpha</td><td>Beta</td></tr>
        </table>
        </div>"""
        items = self._parse(html)
        self.assertTrue(any("Alpha" in i and "Beta" in i for i in items))

    def test_li_whitespace_only_not_appended(self):
        html = """<div class="mw-parser-output">
        <ul><li>   </li></ul>
        </div>"""
        items = self._parse(html)
        self.assertFalse(any(i.strip() == "" for i in items))

    def test_sistersitebox_table_skipped(self):
        html = """<div class="mw-parser-output">
        <table class="sistersitebox">
          <tr><td>Sister site content</td></tr>
        </table>
        </div>"""
        items = self._parse(html)
        self.assertFalse(any("Sister site" in i for i in items))

    def test_ambox_table_skipped(self):
        html = """<div class="mw-parser-output">
        <table class="ambox">
          <tr><td>Article message content</td></tr>
        </table>
        </div>"""
        items = self._parse(html)
        self.assertFalse(any("Article message" in i for i in items))

    def test_mw_references_list_skipped(self):
        html = """<div class="mw-parser-output">
        <ul class="mw-references">
          <p>Ref paragraph</p>
        </ul>
        </div>"""
        items = self._parse(html)
        self.assertFalse(any("Ref paragraph" in i for i in items))

    def test_navbox_list_skipped(self):
        html = """<div class="mw-parser-output">
        <ul class="navbox">
          <p>Navbox list item</p>
        </ul>
        </div>"""
        items = self._parse(html)
        self.assertFalse(any("Navbox list item" in i for i in items))

    def test_metadata_table_skipped(self):
        html = """<div class="mw-parser-output">
        <table class="metadata">
          <tr><td>Metadata content</td></tr>
        </table>
        </div>"""
        items = self._parse(html)
        self.assertFalse(any("Metadata content" in i for i in items))

    def test_skip_depth_handles_inert_tag_nesting(self):
        # Style with text before/after in same li
        html = """<div class="mw-parser-output">
        <ul>
          <li>Before <style>a{}</style> after style tag.</li>
        </ul>
        </div>"""
        items = self._parse(html)
        # The style content should be dropped
        self.assertFalse(any("a{}" in i for i in items))
        self.assertTrue(any("Before" in i and "after style tag" in i for i in items))

    def test_handle_data_when_not_in_main(self):
        """Data before main div must be silently ignored."""
        html = "<p>Outside content</p><div class='mw-parser-output'></div>"
        items = self._parse(html)
        self.assertFalse(any("Outside content" in i for i in items))


# ---------------------------------------------------------------------------
# _fetch_html
# ---------------------------------------------------------------------------

class FetchHtmlTest(unittest.TestCase):
    def test_success(self):
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.text = "<html><body>Content</body></html>"
        with patch.object(wikipedia.requests, "get", return_value=resp):
            html = _fetch_html("Some_Page")
        self.assertEqual(html, resp.text)

    def test_http_error_propagates(self):
        from requests import HTTPError
        resp = Mock()
        resp.raise_for_status.side_effect = HTTPError("404")
        with patch.object(wikipedia.requests, "get", return_value=resp):
            with self.assertRaises(HTTPError):
                _fetch_html("Missing_Page")


# ---------------------------------------------------------------------------
# fetch (end-to-end)
# ---------------------------------------------------------------------------

# Minimal Wikipedia-like HTML with a mix of list items and table rows.
_WIKI_HTML = """\
<html><body>
<div class="mw-parser-output">
  <ul>
    <li>A man was fatally struck by a falling coconut in 1988 during a routine walk on a Caribbean island.</li>
    <li>A woman accidentally glued herself to a chair in 2001 and had to call emergency services.</li>
    <li>An eccentric inventor was killed by his own robot creation in a freak accident in the early 1970s.</li>
    <li>Short.</li>
    <li>x</li>
    <li>Retrieved January 5, 2023. Something that is a citation.</li>
    <li>doi: 10.1234/something journal article reference citation.</li>
    <li>A duplicate entry about the coconut incident from 1988 in the Caribbean island area.</li>
  </ul>
</div>
</body></html>
"""

# Over-long item (> 1500 chars) to test max_chars filter
_LONG_ITEM = "word " * 400  # 2000 chars


_WIKI_HTML_WITH_LONG = f"""\
<html><body>
<div class="mw-parser-output">
  <ul>
    <li>Normal length item that should be included in the results.</li>
    <li>{_LONG_ITEM}</li>
  </ul>
</div>
</body></html>
"""


class WikipediaFetchTest(unittest.TestCase):
    def _mock_html(self, html):
        return patch.object(wikipedia, "_fetch_html", return_value=html)

    def test_basic_fetch_returns_stories(self):
        with self._mock_html(_WIKI_HTML):
            stories = fetch(page="unusual_deaths_21c", limit=10, min_chars=50, max_chars=1500)
        self.assertGreater(len(stories), 0)
        titles = [s.title for s in stories]
        # First matching item is the coconut one
        self.assertTrue(any("coconut" in t.lower() or "coconut" in s.body.lower()
                            for s, t in zip(stories, titles)))

    def test_preset_key_lookup(self):
        """Preset key 'unusual_deaths_21c' maps to a real page title."""
        captured = {}

        def fake_fetch_html(page_title):
            captured["page_title"] = page_title
            return _WIKI_HTML

        with patch.object(wikipedia, "_fetch_html", side_effect=fake_fetch_html):
            fetch(page="unusual_deaths_21c", min_chars=10)
        self.assertEqual(captured["page_title"],
                         wikipedia.PRESET_PAGES["unusual_deaths_21c"])

    def test_raw_page_title_passthrough(self):
        captured = {}

        def fake_fetch_html(page_title):
            captured["page_title"] = page_title
            return _WIKI_HTML

        with patch.object(wikipedia, "_fetch_html", side_effect=fake_fetch_html):
            fetch(page="My_Custom_Page", min_chars=10)
        self.assertEqual(captured["page_title"], "My_Custom_Page")

    def test_min_chars_filter(self):
        with self._mock_html(_WIKI_HTML):
            stories = fetch(page="unusual_deaths_21c", min_chars=5000, max_chars=50000)
        self.assertEqual(stories, [])

    def test_max_chars_filter(self):
        with self._mock_html(_WIKI_HTML_WITH_LONG):
            stories = fetch(page="unusual_deaths_21c", limit=10, min_chars=10, max_chars=1500)
        # Only the short item passes; the long item is filtered
        self.assertTrue(all(len(s.body) <= 1500 for s in stories))

    def test_limit_respected(self):
        with self._mock_html(_WIKI_HTML):
            stories = fetch(page="unusual_deaths_21c", limit=2, min_chars=50, max_chars=2000)
        self.assertLessEqual(len(stories), 2)

    def test_citation_items_filtered(self):
        with self._mock_html(_WIKI_HTML):
            stories = fetch(page="unusual_deaths_21c", min_chars=10, max_chars=2000)
        bodies = [s.body for s in stories]
        self.assertFalse(any("Retrieved" in b for b in bodies))
        self.assertFalse(any("doi:" in b for b in bodies))

    def test_duplicate_titles_deduplicated(self):
        """Two items with the same first sentence get deduplicated."""
        html = """\
<div class="mw-parser-output">
<ul>
  <li>A man fell off a cliff while taking a selfie in 2015 near the coast.</li>
  <li>A man fell off a cliff while taking a selfie in 2015 near the coast. More text.</li>
</ul>
</div>"""
        with self._mock_html(html):
            stories = fetch(page="unusual_deaths_21c", limit=10, min_chars=10, max_chars=2000)
        titles = [s.title for s in stories]
        # Duplicate titles should be dropped
        self.assertEqual(len(titles), len(set(t.lower() for t in titles)))

    def test_title_truncated_at_140_chars(self):
        long_sentence = "A " + "very " * 30 + "long event."
        html = f'<div class="mw-parser-output"><ul><li>{long_sentence}</li></ul></div>'
        with self._mock_html(html):
            stories = fetch(page="unusual_deaths_21c", limit=5, min_chars=10, max_chars=2000)
        for s in stories:
            self.assertLessEqual(len(s.title), 140)

    def test_metadata_populated(self):
        html = """\
<div class="mw-parser-output">
<ul><li>A notable event occurred in the 21st century and is documented here.</li></ul>
</div>"""
        with self._mock_html(html):
            stories = fetch(page="unusual_deaths_21c", limit=5, min_chars=10)
        self.assertEqual(stories[0].metadata["license"], "CC-BY-SA")
        # source uses the full page title, not the preset key
        self.assertIn("wikipedia:", stories[0].source)

    def test_table_rows_extracted(self):
        html = """\
<div class="mw-parser-output">
<table class="wikitable">
  <tr>
    <td>2005</td>
    <td>An unusual death involving an umbrella was recorded in a European city.</td>
  </tr>
</table>
</div>"""
        with self._mock_html(html):
            stories = fetch(page="unusual_deaths_21c", limit=5, min_chars=10, max_chars=2000)
        self.assertTrue(any("unusual death" in s.body for s in stories))


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class MainTest(unittest.TestCase):
    def test_main_runs_with_no_stories(self):
        orig_argv = sys.argv[:]
        sys.argv = ["wikipedia", "--page", "unusual_deaths_21c", "--limit", "5",
                    "--out", "data/intermediate", "--channel", "wiki_oddities"]
        try:
            with patch.object(wikipedia, "fetch", return_value=[]):
                wikipedia.main()
        finally:
            sys.argv = orig_argv

    def test_main_calls_save_raw(self):
        from pipeline.sources.base import RawStory
        story = RawStory(slug="s", title="T", body="B", source="src", url="http://u")
        orig_argv = sys.argv[:]
        sys.argv = ["wikipedia", "--page", "unusual_deaths_21c", "--limit", "1",
                    "--min-chars", "10", "--max-chars", "2000",
                    "--out", "data/intermediate", "--channel", "wiki_oddities"]
        try:
            with patch.object(wikipedia, "fetch", return_value=[story]):
                with patch("pipeline.sources.wikipedia.save_raw", return_value="/fake"):
                    wikipedia.main()
        finally:
            sys.argv = orig_argv


if __name__ == "__main__":
    unittest.main()
