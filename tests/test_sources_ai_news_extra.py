"""Extra tests to push pipeline.sources.ai_news to 100% line coverage.

The existing test_pipeline_ai_news.py covers the happy paths.
This file covers the remaining branches identified in the coverage report:
  - _hn_fetch_item: non-dict response (line 113)
  - _hn_fetch_item: RequestException (lines 117-118)
  - _qualifies_as_ai: ValueError on URL parse (lines 130-131)
  - _fetch_url_body: non-html content type (line 166)
  - _fetch_url_body: body truncation at URL_FETCH_MAX_BYTES (line 173)
  - _fetch_url_body: title prepended when text doesn't start with it (line 182)
  - _fetch_url_body: RequestException returns "" (line 176)
  - fetch(): top-ids RequestException → empty list (lines 209-211)
  - fetch(): item with empty title → continue (line 227)
  - main(): all branches (lines 297-330)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.sources import ai_news


class _FakeResponse:
    def __init__(self, *, json_payload=None, text="", headers=None,
                 status_code=200, iter_chunks=None):
        self._json = json_payload
        self.text = text
        self.headers = headers or {}
        self.status_code = status_code
        self._iter_chunks = iter_chunks or []

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(f"{self.status_code}")

    def json(self):
        return self._json

    def iter_content(self, chunk_size=8192):
        yield from self._iter_chunks


# ---------------------------------------------------------------------------
# _hn_fetch_item edge cases
# ---------------------------------------------------------------------------

class HnFetchItemEdgeCasesTest(unittest.TestCase):
    def test_non_dict_response_returns_none(self):
        """When HN returns a non-dict (e.g. null), _hn_fetch_item returns None."""
        resp = _FakeResponse(json_payload=[1, 2, 3])  # list, not dict
        with patch.object(ai_news.requests, "get", return_value=resp):
            result = ai_news._hn_fetch_item(12345)
        self.assertIsNone(result)

    def test_request_exception_returns_none(self):
        from requests import RequestException
        with patch.object(ai_news.requests, "get",
                          side_effect=RequestException("timeout")):
            result = ai_news._hn_fetch_item(99999)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _qualifies_as_ai: ValueError on invalid URL
# ---------------------------------------------------------------------------

class QualifiesAsAIValueErrorTest(unittest.TestCase):
    def test_malformed_url_suppressed(self):
        """IPv6-bracket URL may raise ValueError on .hostname; should return False."""
        # Patch urlparse to simulate ValueError from .hostname.
        # The title must NOT match any AI keyword so we reach the URL branch.

        class _BadResult:
            @property
            def hostname(self):
                raise ValueError("invalid IPv6 URL")

        with patch("pipeline.sources.ai_news.urlparse", return_value=_BadResult()):
            result = ai_news._qualifies_as_ai(
                "Gardening tips for spring planting season",  # no AI keywords
                "http://[invalid]",
            )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# _fetch_url_body edge cases
# ---------------------------------------------------------------------------

class FetchUrlBodyEdgeCasesTest(unittest.TestCase):
    def test_non_html_content_type_returns_empty(self):
        resp = _FakeResponse(
            headers={"Content-Type": "application/pdf"},
            iter_chunks=[b"PDF content"],
        )
        with patch.object(ai_news.requests, "get", return_value=resp):
            result = ai_news._fetch_url_body("https://example.com/doc.pdf")
        self.assertEqual(result, "")

    def test_large_body_truncated_at_max_bytes(self):
        """iter_content yields a chunk exceeding URL_FETCH_MAX_BYTES → break."""
        big_chunk = b"A" * (ai_news.URL_FETCH_MAX_BYTES + 1000)
        resp = _FakeResponse(
            headers={"Content-Type": "text/html; charset=utf-8"},
            iter_chunks=[big_chunk],
        )
        with patch.object(ai_news.requests, "get", return_value=resp):
            result = ai_news._fetch_url_body("https://example.com/huge")
        # We got something (the chunk was partly used)
        self.assertIsInstance(result, str)

    def test_request_exception_returns_empty(self):
        from requests import RequestException
        with patch.object(ai_news.requests, "get",
                          side_effect=RequestException("no network")):
            result = ai_news._fetch_url_body("https://example.com/page")
        self.assertEqual(result, "")

    def test_title_prepended_when_not_at_text_start(self):
        """Title extracted but body text starts with different content → prepend."""
        html = b"<body>Different content comes first.<title>My Article Title</title></body>"
        resp = _FakeResponse(
            headers={"Content-Type": "text/html; charset=utf-8"},
            iter_chunks=[html],
        )
        with patch.object(ai_news.requests, "get", return_value=resp):
            result = ai_news._fetch_url_body("https://example.com/article")
        # title should be prepended since body starts with "Different", not "My Article"
        self.assertIn("My Article Title", result)
        self.assertIn("Different content", result)

    def test_text_plain_content_type_accepted(self):
        resp = _FakeResponse(
            headers={"Content-Type": "text/plain; charset=utf-8"},
            iter_chunks=[b"Plain text content here."],
        )
        with patch.object(ai_news.requests, "get", return_value=resp):
            result = ai_news._fetch_url_body("https://example.com/readme.txt")
        self.assertIn("Plain text", result)


# ---------------------------------------------------------------------------
# fetch(): top-ids failure → empty list
# ---------------------------------------------------------------------------

class FetchTopIdsMissingTest(unittest.TestCase):
    def test_top_ids_request_exception_returns_empty(self):
        from requests import RequestException

        def _get(url, *args, **kwargs):
            if "topstories" in url:
                raise RequestException("network down")
            return _FakeResponse(json_payload={})

        with patch.object(ai_news.requests, "get", side_effect=_get):
            result = ai_news.fetch(limit=5, hn_top_n=10, fetch_bodies=False)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# fetch(): item with empty title → skip
# ---------------------------------------------------------------------------

class FetchEmptyTitleTest(unittest.TestCase):
    def test_item_with_empty_title_skipped(self):
        ids = [701]
        empty_title_item = {
            "id": 701, "title": "   ", "url": None,
            "score": 50, "by": "user", "time": 1714694400,
            "type": "story", "dead": False, "deleted": False,
        }

        def _get(url, *args, **kwargs):
            if "topstories" in url:
                return _FakeResponse(json_payload=ids)
            if "/item/" in url:
                return _FakeResponse(json_payload=empty_title_item)
            return _FakeResponse(json_payload={})

        with patch.object(ai_news.requests, "get", side_effect=_get):
            result = ai_news.fetch(limit=5, hn_top_n=5, fetch_bodies=False)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# main() — all branches
# ---------------------------------------------------------------------------

class AiNewsMainTest(unittest.TestCase):
    def _run_main(self, argv, mock_stories=None, existing=None):
        """Run ai_news.main() with the given sys.argv."""
        orig_argv = sys.argv[:]
        sys.argv = argv
        try:
            with patch.object(ai_news, "existing_slugs",
                               return_value=set(existing or [])):
                with patch.object(ai_news, "fetch",
                                  return_value=list(mock_stories or [])) as mock_fetch:
                    with patch("pipeline.sources.ai_news.save_raw",
                               return_value=Path("/fake/path")) as mock_save:
                        rc = ai_news.main()
            return rc, mock_fetch, mock_save
        finally:
            sys.argv = orig_argv

    def _make_story(self, n=1):
        from pipeline.sources.base import RawStory
        stories = []
        for i in range(n):
            stories.append(RawStory(
                slug=f"story-{i}",
                title=f"AI Story {i}",
                body=f"Body {i}",
                source="hackernews:topstories",
                url=f"https://openai.com/post{i}",
                metadata={"host": "openai.com", "hn_score": 100},
            ))
        return stories

    def test_main_no_stories_returns_1(self):
        rc, _, _ = self._run_main(
            ["ai_news", "--limit", "5", "--out", "data/test/raw"],
            mock_stories=[],
        )
        self.assertEqual(rc, 1)

    def test_main_with_stories_saves_and_returns_0(self):
        stories = self._make_story(2)
        rc, _, mock_save = self._run_main(
            ["ai_news", "--limit", "5", "--out", "data/test/raw"],
            mock_stories=stories,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mock_save.call_count, 2)

    def test_main_print_only_does_not_save(self):
        stories = self._make_story(1)
        rc, _, mock_save = self._run_main(
            ["ai_news", "--limit", "5", "--print-only", "--out", "data/test/raw"],
            mock_stories=stories,
        )
        self.assertEqual(rc, 0)
        mock_save.assert_not_called()

    def test_main_no_bodies_flag(self):
        stories = self._make_story(1)
        rc, mock_fetch, _ = self._run_main(
            ["ai_news", "--limit", "3", "--no-bodies", "--out", "data/test/raw"],
            mock_stories=stories,
        )
        self.assertEqual(rc, 0)
        call_kwargs = mock_fetch.call_args.kwargs
        self.assertFalse(call_kwargs.get("fetch_bodies"))

    def test_main_skips_existing_slugs_when_present(self):
        stories = self._make_story(1)
        existing = {"old-slug-1", "old-slug-2"}
        rc, mock_fetch, _ = self._run_main(
            ["ai_news", "--limit", "5", "--out", "data/test/raw"],
            mock_stories=stories,
            existing=existing,
        )
        # skip_slugs should be passed to fetch
        call_kwargs = mock_fetch.call_args.kwargs
        self.assertEqual(call_kwargs.get("skip_slugs"), existing)

    def test_main_hn_top_n_flag(self):
        stories = self._make_story(1)
        rc, mock_fetch, _ = self._run_main(
            ["ai_news", "--hn-top-n", "42", "--out", "data/test/raw"],
            mock_stories=stories,
        )
        call_kwargs = mock_fetch.call_args.kwargs
        self.assertEqual(call_kwargs.get("hn_top_n"), 42)


if __name__ == "__main__":
    unittest.main()
