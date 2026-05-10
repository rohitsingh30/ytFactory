"""Tests for pipeline.sources.reddit_api."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import Mock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.sources import reddit_api


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


def _make_child(title, body, *, is_self=True, stickied=False, over_18=False,
                score=100, num_comments=50, author="user", post_id="abc",
                permalink="/r/AITA/comments/abc"):
    return {
        "data": {
            "title": title,
            "selftext": body,
            "is_self": is_self,
            "stickied": stickied,
            "over_18": over_18,
            "score": score,
            "num_comments": num_comments,
            "author": author,
            "id": post_id,
            "permalink": permalink,
            "created_utc": 1714694400,
        }
    }


def _make_payload(children):
    return {"data": {"children": children}}


class FetchTest(unittest.TestCase):
    def _mock_get(self, payload, status_code=200):
        resp = _FakeResponse(json_payload=payload, status_code=status_code)
        return patch.object(reddit_api.requests, "get", return_value=resp)

    def test_happy_path(self):
        body = "x" * 500
        payload = _make_payload([_make_child("My Story", body)])
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole", limit=5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "My Story")
        self.assertEqual(out[0].source, "reddit:AmItheAsshole")
        self.assertIn("reddit.com", out[0].url)

    def test_stickied_posts_skipped(self):
        body = "x" * 500
        payload = _make_payload([_make_child("Stickied", body, stickied=True)])
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole")
        self.assertEqual(out, [])

    def test_non_self_post_skipped(self):
        body = "x" * 500
        payload = _make_payload([_make_child("Link", body, is_self=False)])
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole")
        self.assertEqual(out, [])

    def test_nsfw_skipped_when_filter_on(self):
        body = "x" * 500
        payload = _make_payload([_make_child("NSFW", body, over_18=True)])
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole", skip_nsfw=True)
        self.assertEqual(out, [])

    def test_nsfw_included_when_filter_off(self):
        body = "x" * 500
        payload = _make_payload([_make_child("NSFW allowed", body, over_18=True)])
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole", skip_nsfw=False)
        self.assertEqual(len(out), 1)

    def test_body_too_short_skipped(self):
        payload = _make_payload([_make_child("Short", "tiny")])
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole", min_chars=400)
        self.assertEqual(out, [])

    def test_body_too_long_skipped(self):
        payload = _make_payload([_make_child("Long", "x" * 10_000)])
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole", max_chars=6000)
        self.assertEqual(out, [])

    def test_limit_respected(self):
        body = "x" * 500
        children = [_make_child(f"Story {i}", body, post_id=str(i)) for i in range(10)]
        payload = _make_payload(children)
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole", limit=3)
        self.assertEqual(len(out), 3)

    def test_http_error_propagates(self):
        from requests import HTTPError
        resp = _FakeResponse(status_code=429)
        with patch.object(reddit_api.requests, "get", return_value=resp):
            with self.assertRaises(HTTPError):
                reddit_api.fetch("AmItheAsshole")

    def test_empty_children(self):
        payload = {"data": {"children": []}}
        with self._mock_get(payload):
            out = reddit_api.fetch("AmItheAsshole")
        self.assertEqual(out, [])

    def test_top_listing_adds_timeframe_param(self):
        body = "x" * 500
        payload = _make_payload([_make_child("Story", body)])
        captured = {}

        def capture_get(url, *, params=None, **kwargs):
            captured["params"] = params
            return _FakeResponse(json_payload=payload)

        with patch.object(reddit_api.requests, "get", side_effect=capture_get):
            reddit_api.fetch("AITA", listing="top", timeframe="week")
        self.assertIn("t", captured["params"])
        self.assertEqual(captured["params"]["t"], "week")

    def test_hot_listing_no_timeframe_param(self):
        body = "x" * 500
        payload = _make_payload([_make_child("Story", body)])
        captured = {}

        def capture_get(url, *, params=None, **kwargs):
            captured["params"] = params
            return _FakeResponse(json_payload=payload)

        with patch.object(reddit_api.requests, "get", side_effect=capture_get):
            reddit_api.fetch("AITA", listing="hot")
        self.assertNotIn("t", captured.get("params", {}))

    def test_metadata_populated(self):
        body = "x" * 500
        payload = _make_payload([_make_child("Story", body, score=999,
                                             num_comments=42, author="bob")])
        with self._mock_get(payload):
            out = reddit_api.fetch("AITA")
        meta = out[0].metadata
        self.assertEqual(meta["score"], 999)
        self.assertEqual(meta["num_comments"], 42)
        self.assertEqual(meta["author"], "bob")


class MainTest(unittest.TestCase):
    def test_main_runs_without_writing_when_no_stories(self):
        orig_argv = sys.argv[:]
        sys.argv = ["reddit_api", "--subreddit", "AITA", "--limit", "5",
                    "--out", "data/intermediate", "--channel", "aita_text"]
        try:
            with patch.object(reddit_api, "fetch", return_value=[]):
                # Should not raise
                reddit_api.main()
        finally:
            sys.argv = orig_argv

    def test_main_calls_save_raw_for_each_story(self):
        from pipeline.sources.base import RawStory
        story = RawStory(slug="s1", title="T", body="B", source="src", url="http://u")
        orig_argv = sys.argv[:]
        sys.argv = ["reddit_api", "--subreddit", "AITA", "--limit", "1",
                    "--out", "data/intermediate", "--channel", "aita_text"]
        try:
            with patch.object(reddit_api, "fetch", return_value=[story]):
                with patch("pipeline.sources.reddit_api.save_raw", return_value="/fake/path") as mock_save:
                    reddit_api.main()
            mock_save.assert_called_once()
        finally:
            sys.argv = orig_argv


if __name__ == "__main__":
    unittest.main()
