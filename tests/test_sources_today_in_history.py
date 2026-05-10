"""Tests for pipeline.sources.today_in_history."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.sources import today_in_history


class _FakeResponse:
    def __init__(self, *, json_payload=None, status_code=200):
        self._json = json_payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(f"{self.status_code}")

    def json(self):
        return self._json


def _make_event(text, year=2000, *, pages=None):
    ev = {"text": text, "year": year}
    if pages is not None:
        ev["pages"] = pages
    return ev


def _make_page(title, extract="", *, content_urls=None):
    page = {"normalizedtitle": title, "extract": extract}
    if content_urls is not None:
        page["content_urls"] = content_urls
    return page


class FetchTest(unittest.TestCase):
    def _mock_get(self, payload, status_code=200):
        resp = _FakeResponse(json_payload=payload, status_code=status_code)
        return patch.object(today_in_history.requests, "get", return_value=resp)

    def test_happy_path(self):
        ev = _make_event(
            "Something remarkable happened in history on this day and it was well-documented.",
            year=1969,
        )
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=7, day=20, min_chars=10)
        self.assertEqual(len(out), 1)
        self.assertIn("1969", out[0].title)
        self.assertIn("wikipedia:onthisday", out[0].source)

    def test_auto_today_when_no_month_day(self):
        """Passing month=None/day=None uses today's date."""
        ev = _make_event("Auto-date event that has enough text to pass the filter.", year=2020)
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            # Neither month nor day supplied — uses today
            out = today_in_history.fetch(min_chars=40)
        self.assertGreater(len(out), 0)

    def test_auto_month_only(self):
        """Passing day=None but month specified uses today's day."""
        ev = _make_event("Event text long enough to pass min chars filter here.", year=1900)
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=1, min_chars=40)
        self.assertGreater(len(out), 0)

    def test_event_too_short_skipped(self):
        ev = _make_event("Short", year=2000)
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=1, day=1, min_chars=80)
        self.assertEqual(out, [])

    def test_event_with_page_extract_included_in_body(self):
        page = _make_page("Moon Landing", "Neil Armstrong landed on the moon.")
        ev = _make_event("The first lunar landing occurred.", year=1969, pages=[page])
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=7, day=20, min_chars=10)
        self.assertIn("Neil Armstrong", out[0].body)

    def test_event_page_extract_same_as_text_not_duplicated(self):
        text = "The first lunar landing occurred in 1969 and it was historic."
        page = _make_page("Moon Landing", text)  # same as text → skip
        ev = _make_event(text, year=1969, pages=[page])
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=7, day=20, min_chars=10)
        # extract == text → not appended again
        self.assertEqual(out[0].body.count(text), 1)

    def test_event_without_pages(self):
        ev = _make_event("An event with no linked page at all, long enough.", year=1945)
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=5, day=8, min_chars=10)
        self.assertEqual(len(out), 1)
        # URL falls back to anniversary URL
        self.assertIn("wikipedia.org", out[0].url)

    def test_event_with_content_url(self):
        content_urls = {"desktop": {"page": "https://en.wikipedia.org/wiki/Moon_landing"}}
        page = _make_page("Moon Landing", content_urls=content_urls)
        ev = _make_event("The first lunar landing occurred.", year=1969, pages=[page])
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=7, day=20, min_chars=10)
        self.assertEqual(out[0].url, "https://en.wikipedia.org/wiki/Moon_landing")

    def test_event_without_content_url_uses_fallback(self):
        page = _make_page("Moon Landing")  # no content_urls
        ev = _make_event("The first lunar landing.", year=1969, pages=[page])
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=7, day=20, min_chars=10)
        self.assertIn("Selected_anniversaries", out[0].url)

    def test_event_page_title_fallback_to_title(self):
        """normalizedtitle missing → fall back to 'title' key."""
        page = {"title": "RawTitle", "extract": "Some extract text."}
        ev = _make_event("An event with a page.", year=2000, pages=[page])
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=1, day=1, min_chars=10)
        self.assertEqual(out[0].metadata["primary_page"], "RawTitle")

    def test_limit_respected(self):
        events = [_make_event(f"Event number {i} with enough text.", year=1900 + i)
                  for i in range(20)]
        payload = {"selected": events}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=6, day=15, limit=5, min_chars=10)
        self.assertEqual(len(out), 5)

    def test_fallback_to_events_key(self):
        """If feed_type key missing in payload, fall back to 'events' key."""
        ev = _make_event("An event from the events key, long enough to pass.", year=1800)
        # feed_type="selected" but key missing → use "events"
        payload = {"events": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=3, day=10, feed_type="selected", min_chars=10)
        self.assertEqual(len(out), 1)

    def test_event_without_year(self):
        """year=None means 'x' in the slug and no year in title."""
        ev = {"text": "Something notable happened long ago and is documented here.", "year": None}
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=1, day=1, min_chars=10)
        self.assertEqual(len(out), 1)
        # slug contains 'x' for year
        self.assertIn("-x-", out[0].slug)
        # Title uses text[:120] without year prefix
        self.assertNotIn("In None", out[0].title)

    def test_http_error_propagates(self):
        from requests import HTTPError
        resp = _FakeResponse(status_code=404)
        with patch.object(today_in_history.requests, "get", return_value=resp):
            with self.assertRaises(HTTPError):
                today_in_history.fetch(month=1, day=1)

    def test_metadata_populated(self):
        ev = _make_event("Something happened in history.", year=1066)
        payload = {"selected": [ev]}
        with self._mock_get(payload):
            out = today_in_history.fetch(month=10, day=14, min_chars=10)
        meta = out[0].metadata
        self.assertEqual(meta["year"], 1066)
        self.assertEqual(meta["month"], 10)
        self.assertEqual(meta["day"], 14)
        self.assertEqual(meta["license"], "CC-BY-SA")


class MainTest(unittest.TestCase):
    def test_main_runs(self):
        from pipeline.sources.base import RawStory
        story = RawStory(slug="s", title="T", body="B", source="src", url="http://u")
        orig_argv = sys.argv[:]
        sys.argv = ["today_in_history", "--month", "1", "--day", "1",
                    "--limit", "1", "--out", "data/intermediate", "--channel", "tih"]
        try:
            with patch.object(today_in_history, "fetch", return_value=[story]):
                with patch("pipeline.sources.today_in_history.save_raw", return_value="/fake"):
                    today_in_history.main()
        finally:
            sys.argv = orig_argv

    def test_main_no_stories(self):
        orig_argv = sys.argv[:]
        sys.argv = ["today_in_history", "--month", "1", "--day", "1",
                    "--limit", "1", "--out", "data/intermediate", "--channel", "tih"]
        try:
            with patch.object(today_in_history, "fetch", return_value=[]):
                today_in_history.main()
        finally:
            sys.argv = orig_argv


if __name__ == "__main__":
    unittest.main()
