"""Tests for pipeline.sources.ai_news (HN-backed AI news scraper)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.sources import ai_news


# ---- helpers -----------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, *, json_payload=None, text="", headers=None, status_code=200):
        self._json = json_payload
        self.text = text
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError

            raise HTTPError(f"{self.status_code}")

    def json(self):
        return self._json

    def iter_content(self, chunk_size=8192):
        b = self.text.encode("utf-8")
        for i in range(0, len(b), chunk_size):
            yield b[i : i + chunk_size]


def _hn_item(item_id, title, *, url=None, score=100, dead=False, deleted=False, type_="story"):
    return {
        "id": item_id,
        "title": title,
        "url": url,
        "score": score,
        "by": "tester",
        "time": 1714694400,  # arbitrary fixed timestamp
        "descendants": 42,
        "type": type_,
        "dead": dead,
        "deleted": deleted,
    }


# ---- _qualifies_as_ai --------------------------------------------------


class QualifiesAsAITest(unittest.TestCase):
    def test_keyword_in_title(self):
        self.assertTrue(ai_news._qualifies_as_ai("OpenAI ships GPT-5", "https://example.com"))
        self.assertTrue(ai_news._qualifies_as_ai("Anthropic raises $4B", None))
        self.assertTrue(ai_news._qualifies_as_ai("New LLM benchmark released", None))

    def test_anthropic_domain_qualifies_even_without_keyword(self):
        # Title doesn't contain any AI keyword, but the domain is allowlisted.
        self.assertTrue(
            ai_news._qualifies_as_ai("Connectors", "https://www.anthropic.com/news/connectors")
        )

    def test_unrelated_story_rejected(self):
        self.assertFalse(ai_news._qualifies_as_ai("Show HN: my new keyboard", "https://example.com"))
        self.assertFalse(ai_news._qualifies_as_ai("Why I quit my job", None))

    def test_substring_match_does_not_overfire(self):
        # 'plain' contains 'ai' but should not qualify (not a word boundary).
        # The keyword list uses spaces / punctuation around 'ai' to prevent
        # this — verify the fix.
        self.assertFalse(ai_news._qualifies_as_ai("Plain old programming", None))
        self.assertFalse(ai_news._qualifies_as_ai("Mountain biking on trails", None))


# ---- _extract_text_from_html ------------------------------------------


class ExtractHTMLTest(unittest.TestCase):
    def test_strips_tags_and_collapses_whitespace(self):
        html = "<html><body>  <p>Hello   world</p>  <p>Second.</p>  </body></html>"
        out = ai_news._extract_text_from_html(html)
        self.assertEqual(out, "Hello world Second.")

    def test_drops_script_and_style_blocks(self):
        html = (
            "<html><head><script>alert('hi')</script></head>"
            "<body><style>body{color:red}</style>"
            "<p>Real content here</p></body></html>"
        )
        out = ai_news._extract_text_from_html(html)
        self.assertNotIn("alert", out)
        self.assertNotIn("color:red", out)
        self.assertIn("Real content here", out)

    def test_caps_max_chars(self):
        html = "<p>" + ("x" * 10_000) + "</p>"
        out = ai_news._extract_text_from_html(html, max_chars=200)
        self.assertEqual(len(out), 200)


# ---- fetch (end-to-end with mocked HTTP) -------------------------------


class FetchTest(unittest.TestCase):
    def _patched_get(self, top_ids, items_by_id, url_bodies=None):
        """Build a requests.get mock that serves HN + URL fetch responses."""
        url_bodies = url_bodies or {}

        def _get(url, *args, **kwargs):
            if url.endswith("/topstories.json"):
                return _FakeResponse(json_payload=top_ids)
            if "/item/" in url and url.endswith(".json"):
                # /v0/item/<id>.json
                item_id = int(url.rsplit("/", 1)[-1].rsplit(".", 1)[0])
                return _FakeResponse(json_payload=items_by_id.get(item_id))
            # Article-body fetch
            body = url_bodies.get(url, "")
            return _FakeResponse(
                text=body,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )

        return patch.object(ai_news.requests, "get", side_effect=_get)

    def test_filters_to_ai_stories(self):
        ids = [101, 102, 103, 104]
        items = {
            101: _hn_item(101, "OpenAI announces GPT-5.5", url="https://openai.com/blog/gpt55"),
            102: _hn_item(102, "Why I bought a tractor"),  # not AI
            103: _hn_item(103, "New Anthropic Claude release", url="https://www.anthropic.com/news/x"),
            104: _hn_item(104, "Show HN: pretty terminal"),  # not AI
        }
        with self._patched_get(ids, items, url_bodies={
            "https://openai.com/blog/gpt55": "<title>GPT-5.5</title><p>Released today.</p>",
            "https://www.anthropic.com/news/x": "<title>Claude X</title><p>Now available.</p>",
        }):
            out = ai_news.fetch(limit=10, hn_top_n=10, fetch_bodies=True)

        slugs = {s.slug for s in out}
        self.assertEqual(len(out), 2)
        # Both AI stories made it; non-AI were filtered.
        titles = sorted(s.title for s in out)
        self.assertEqual(titles, ["New Anthropic Claude release", "OpenAI announces GPT-5.5"])
        # Slugs are deterministic shape, not snapshotted to date specifics
        for sl in slugs:
            self.assertTrue(len(sl) > 0)

    def test_dead_and_deleted_items_skipped(self):
        ids = [201, 202, 203]
        items = {
            201: _hn_item(201, "OpenAI big news", dead=True),
            202: _hn_item(202, "Anthropic update", deleted=True),
            203: _hn_item(203, "Google AI ships Gemini 3"),
        }
        with self._patched_get(ids, items):
            out = ai_news.fetch(limit=10, hn_top_n=10, fetch_bodies=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "Google AI ships Gemini 3")

    def test_dedups_within_run(self):
        ids = [301, 302]
        # Two HN posts pointing at the same URL — keep one.
        items = {
            301: _hn_item(301, "OpenAI ships GPT", url="https://openai.com/x"),
            302: _hn_item(302, "OpenAI ships GPT (mirror)", url="https://openai.com/x"),
        }
        with self._patched_get(ids, items, url_bodies={"https://openai.com/x": "<p>a</p>"}):
            out = ai_news.fetch(limit=10, hn_top_n=10, fetch_bodies=True)
        self.assertEqual(len(out), 1)

    def test_skip_slugs_dedup_against_disk(self):
        ids = [401]
        items = {401: _hn_item(401, "OpenAI blah", url="https://openai.com/y")}
        # Pre-compute the slug fetch() would produce, then ask it to skip.
        from datetime import datetime, timezone


        with self._patched_get(ids, items, url_bodies={"https://openai.com/y": "<p>a</p>"}):
            once = ai_news.fetch(limit=10, hn_top_n=10, fetch_bodies=True)
            self.assertEqual(len(once), 1)
            again = ai_news.fetch(
                limit=10, hn_top_n=10, fetch_bodies=False,
                skip_slugs={once[0].slug},
            )
        self.assertEqual(len(again), 0)

    def test_respects_limit_after_filter(self):
        ids = list(range(501, 520))
        items = {
            i: _hn_item(i, f"AI breakthrough number {i}", url=f"https://openai.com/p{i}")
            for i in ids
        }
        with self._patched_get(ids, items):
            out = ai_news.fetch(limit=3, hn_top_n=20, fetch_bodies=False)
        self.assertEqual(len(out), 3)

    def test_url_fetch_failure_falls_back_to_title_only(self):
        """If body fetch fails, the story still ships — body = title only."""
        ids = [601]
        items = {601: _hn_item(601, "OpenAI tease GPT-6", url="https://openai.com/tease")}

        def _get(url, *args, **kwargs):
            if url.endswith("/topstories.json"):
                return _FakeResponse(json_payload=ids)
            if "/item/" in url:
                return _FakeResponse(json_payload=items[601])
            # The article fetch raises.
            from requests import RequestException
            raise RequestException("network down")

        with patch.object(ai_news.requests, "get", side_effect=_get):
            out = ai_news.fetch(limit=5, hn_top_n=5, fetch_bodies=True)
        self.assertEqual(len(out), 1)
        # Body should still contain the title and the HN discussion link
        self.assertIn("OpenAI tease GPT-6", out[0].body)
        self.assertIn("HN discussion", out[0].body)


# ---- existing_slugs ----------------------------------------------------


class ExistingSlugsTest(unittest.TestCase):
    def test_reads_slug_from_each_json(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            (dest / "a.json").write_text(json.dumps({"slug": "foo", "title": "x"}))
            (dest / "b.json").write_text(json.dumps({"slug": "bar", "title": "y"}))
            slugs = ai_news.existing_slugs(dest)
        self.assertEqual(slugs, {"foo", "bar"})

    def test_empty_dir_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            slugs = ai_news.existing_slugs(Path(d))
        self.assertEqual(slugs, set())

    def test_missing_dir_returns_empty_set(self):
        slugs = ai_news.existing_slugs(Path("/tmp/definitely-does-not-exist-aaaa"))
        self.assertEqual(slugs, set())

    def test_corrupt_json_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            (dest / "good.json").write_text(json.dumps({"slug": "ok"}))
            (dest / "bad.json").write_text("not json {")
            slugs = ai_news.existing_slugs(dest)
        self.assertEqual(slugs, {"ok"})


if __name__ == "__main__":
    unittest.main()
