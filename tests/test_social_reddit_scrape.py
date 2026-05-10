"""Tests for pipeline.social.reddit_scrape — 100% line coverage."""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.social.reddit_scrape as reddit_scrape


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _urlopen_mock(data: dict):
    """Return a context-manager mock for urllib.request.urlopen."""
    body = json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=resp)


def _make_listing(children: list[dict]) -> dict:
    return {"data": {"children": [{"data": c} for c in children]}}


def _good_post(**kwargs) -> dict:
    base = {
        "id": "abc123",
        "stickied": False,
        "over_18": False,
        "locked": False,
        "score": 10_000,
        "num_comments": 500,
        "is_self": True,
        "permalink": "/r/AmItheAsshole/comments/abc123/test/",
        "subreddit": "AmItheAsshole",
        "title": "AITA for doing X?",
        "selftext": "Here is the story",
        "author": "testuser",
        "upvote_ratio": 0.95,
        "created_utc": 1714694400.0,
    }
    base.update(kwargs)
    return base


def _comment_listing(comments: list[dict]) -> list[dict]:
    """Two-element listing: [post_listing, comments_listing]."""
    post = _good_post()
    post_listing = {
        "data": {"children": [{"data": post}]}
    }
    comments_listing = {
        "data": {"children": comments}
    }
    return [post_listing, comments_listing]


def _t1_comment(**kwargs) -> dict:
    base = {
        "kind": "t1",
        "data": {
            "id": "cmt1",
            "author": "commenter",
            "body": "This is a great comment!",
            "score": 5000,
            "depth": 0,
            "controversiality": 0,
            "stickied": False,
            "permalink": "/r/AmItheAsshole/comments/abc123/test/cmt1/",
        },
    }
    base["data"].update(kwargs)
    return base


# ---------------------------------------------------------------------------
# _get
# ---------------------------------------------------------------------------

class GetTest(unittest.TestCase):
    def test_returns_parsed_json(self):
        data = {"foo": "bar", "n": 42}
        with patch("urllib.request.urlopen", _urlopen_mock(data)):
            result = reddit_scrape._get("https://example.com/test.json")
        self.assertEqual(result, data)


# ---------------------------------------------------------------------------
# fetch_top_post
# ---------------------------------------------------------------------------

class FetchTopPostTest(unittest.TestCase):
    def _patch(self, children):
        listing = _make_listing(children)
        return patch("urllib.request.urlopen", _urlopen_mock(listing))

    def test_returns_qualifying_post(self):
        with self._patch([_good_post()]):
            post = reddit_scrape.fetch_top_post("AmItheAsshole")
        self.assertIsNotNone(post)
        self.assertEqual(post["id"], "abc123")

    def test_skips_stickied(self):
        posts = [_good_post(stickied=True), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole")
        self.assertEqual(post["id"], "xyz999")

    def test_skips_nsfw(self):
        posts = [_good_post(over_18=True), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole")
        self.assertEqual(post["id"], "xyz999")

    def test_skips_locked(self):
        posts = [_good_post(locked=True), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole")
        self.assertEqual(post["id"], "xyz999")

    def test_skips_id_in_skip_set(self):
        posts = [_good_post(id="abc123"), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole", skip_ids={"abc123"})
        self.assertEqual(post["id"], "xyz999")

    def test_skips_low_score(self):
        posts = [_good_post(score=100), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole", min_score=5000)
        self.assertEqual(post["id"], "xyz999")

    def test_skips_low_comments(self):
        posts = [_good_post(num_comments=10), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole", min_comments=200)
        self.assertEqual(post["id"], "xyz999")

    def test_skips_link_post(self):
        posts = [_good_post(is_self=False), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole")
        self.assertEqual(post["id"], "xyz999")

    def test_returns_none_when_nothing_passes(self):
        posts = [_good_post(stickied=True), _good_post(over_18=True)]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole")
        self.assertIsNone(post)

    def test_hot_sort_no_time_window(self):
        """'hot' sort must NOT append &t= to the URL."""
        with self._patch([_good_post()]) as mock_urlopen:
            reddit_scrape.fetch_top_post("AmItheAsshole", sort="hot")
        call_args = mock_urlopen.call_args
        req_obj = call_args[0][0]
        self.assertNotIn("&t=", req_obj.full_url)

    def test_top_sort_appends_time_window(self):
        with self._patch([_good_post()]) as mock_urlopen:
            reddit_scrape.fetch_top_post("AmItheAsshole", sort="top", time_window="week")
        call_args = mock_urlopen.call_args
        req_obj = call_args[0][0]
        self.assertIn("&t=week", req_obj.full_url)

    def test_score_none_treated_as_zero(self):
        posts = [_good_post(score=None), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole", min_score=5000)
        self.assertEqual(post["id"], "xyz999")

    def test_num_comments_none_treated_as_zero(self):
        posts = [_good_post(num_comments=None), _good_post(id="xyz999")]
        with self._patch(posts):
            post = reddit_scrape.fetch_top_post("AmItheAsshole", min_comments=200)
        self.assertEqual(post["id"], "xyz999")


# ---------------------------------------------------------------------------
# fetch_post_with_comments
# ---------------------------------------------------------------------------

class FetchPostWithCommentsTest(unittest.TestCase):
    def _call(self, comment_children):
        raw = _comment_listing(comment_children)
        resp_body = json.dumps(raw).encode()
        resp = MagicMock()
        resp.read.return_value = resp_body
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            return reddit_scrape.fetch_post_with_comments(
                "AmItheAsshole", "abc123", top_n_comments=5
            )

    def test_basic_comment_included(self):
        result = self._call([_t1_comment()])
        self.assertEqual(len(result["top_comments"]), 1)
        self.assertEqual(result["top_comments"][0]["author"], "commenter")

    def test_non_t1_kind_skipped(self):
        non_t1 = {"kind": "more", "data": {"id": "x", "body": "...", "score": 999}}
        result = self._call([non_t1, _t1_comment()])
        self.assertEqual(len(result["top_comments"]), 1)

    def test_stickied_comment_skipped(self):
        result = self._call([_t1_comment(stickied=True), _t1_comment(id="cmt2")])
        self.assertEqual(len(result["top_comments"]), 1)
        self.assertEqual(result["top_comments"][0]["id"], "cmt2")

    def test_empty_body_skipped(self):
        result = self._call([_t1_comment(body=""), _t1_comment(id="cmt2")])
        self.assertEqual(len(result["top_comments"]), 1)

    def test_deleted_body_skipped(self):
        result = self._call([_t1_comment(body="[deleted]"), _t1_comment(id="cmt2")])
        self.assertEqual(len(result["top_comments"]), 1)

    def test_removed_body_skipped(self):
        result = self._call([_t1_comment(body="[removed]"), _t1_comment(id="cmt2")])
        self.assertEqual(len(result["top_comments"]), 1)

    def test_comments_sorted_by_score_descending(self):
        c1 = _t1_comment(id="low", score=100)
        c2 = _t1_comment(id="high", score=9999)
        result = self._call([c1, c2])
        self.assertEqual(result["top_comments"][0]["id"], "high")

    def test_top_n_comments_respected(self):
        comments = [_t1_comment(id=f"c{i}", score=i) for i in range(10)]
        result = self._call(comments)
        self.assertLessEqual(len(result["top_comments"]), 5)

    def test_post_fields_populated(self):
        result = self._call([])
        p = result["post"]
        self.assertEqual(p["subreddit"], "AmItheAsshole")
        self.assertIn("reddit.com", p["url"])
        self.assertEqual(p["is_nsfw"], False)
        self.assertEqual(p["is_locked"], False)

    def test_author_none_falls_back_to_unknown(self):
        raw = _comment_listing([])
        # Null author on post
        raw[0]["data"]["children"][0]["data"]["author"] = None
        resp_body = json.dumps(raw).encode()
        resp = MagicMock()
        resp.read.return_value = resp_body
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            result = reddit_scrape.fetch_post_with_comments("AmItheAsshole", "abc123")
        self.assertEqual(result["post"]["author"], "[unknown]")

    def test_comment_author_none_falls_back(self):
        c = _t1_comment()
        c["data"]["author"] = None
        result = self._call([c])
        self.assertEqual(result["top_comments"][0]["author"], "[unknown]")

    def test_comment_score_none_treated_as_zero(self):
        c = _t1_comment(score=None)
        result = self._call([c])
        self.assertEqual(result["top_comments"][0]["score"], 0)


# ---------------------------------------------------------------------------
# _parse_thread_url
# ---------------------------------------------------------------------------

class ParseThreadUrlTest(unittest.TestCase):
    def test_valid_url(self):
        url = "https://www.reddit.com/r/AmItheAsshole/comments/abc123/my_title/"
        sub, pid = reddit_scrape._parse_thread_url(url)
        self.assertEqual(sub, "AmItheAsshole")
        self.assertEqual(pid, "abc123")

    def test_invalid_too_short(self):
        with self.assertRaises(SystemExit):
            reddit_scrape._parse_thread_url("https://www.reddit.com/r/sub/")

    def test_invalid_not_r_prefix(self):
        with self.assertRaises(SystemExit):
            reddit_scrape._parse_thread_url("https://www.reddit.com/x/sub/comments/abc/slug/")

    def test_invalid_not_comments(self):
        with self.assertRaises(SystemExit):
            reddit_scrape._parse_thread_url("https://www.reddit.com/r/sub/posts/abc/slug/")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class MainTest(unittest.TestCase):
    def _bundle(self):
        return {
            "post": {
                "id": "abc123",
                "subreddit": "AmItheAsshole",
                "title": "AITA for X",
                "score": 10000,
                "num_comments": 500,
                "selftext": "My story",
            },
            "top_comments": [],
        }

    @patch("pipeline.social.reddit_scrape.fetch_post_with_comments")
    @patch("pipeline.social.reddit_scrape.fetch_top_post")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.mkdir")
    def test_main_subreddit_path(self, mock_mkdir, mock_write, mock_top, mock_comments):
        mock_top.return_value = _good_post()
        mock_comments.return_value = self._bundle()
        with patch("sys.argv", [
            "prog", "--subreddit", "AmItheAsshole", "--out", "_test_out.json"
        ]):
            reddit_scrape.main()
        mock_top.assert_called_once()
        mock_comments.assert_called_once()
        mock_write.assert_called_once()

    @patch("pipeline.social.reddit_scrape.fetch_post_with_comments")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.mkdir")
    def test_main_thread_url_path(self, mock_mkdir, mock_write, mock_comments):
        mock_comments.return_value = self._bundle()
        with patch("sys.argv", [
            "prog",
            "--thread-url", "https://www.reddit.com/r/AmItheAsshole/comments/abc123/slug/",
            "--out", "_test_out.json",
        ]):
            reddit_scrape.main()
        mock_comments.assert_called_once_with("AmItheAsshole", "abc123", top_n_comments=12)
        mock_write.assert_called_once()

    @patch("pipeline.social.reddit_scrape.fetch_top_post")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.mkdir")
    def test_main_no_post_exits(self, mock_mkdir, mock_write, mock_top):
        mock_top.return_value = None
        with patch("sys.argv", [
            "prog", "--subreddit", "AmItheAsshole", "--out", "_test_out.json"
        ]):
            with self.assertRaises(SystemExit) as cm:
                reddit_scrape.main()
        self.assertEqual(cm.exception.code, 1)

    @patch("pipeline.social.reddit_scrape.fetch_post_with_comments")
    @patch("pipeline.social.reddit_scrape.fetch_top_post")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.mkdir")
    def test_main_with_valid_skip_ids_file(self, mock_mkdir, mock_write, mock_top, mock_comments):
        mock_top.return_value = _good_post()
        mock_comments.return_value = self._bundle()
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(["old_id_1", "old_id_2"])
        with patch("sys.argv", [
            "prog", "--subreddit", "AmItheAsshole", "--out", "_test_out.json",
            "--skip-ids-file", "fake_skip.json",
        ]):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value=json.dumps(["old_id_1"])):
                    reddit_scrape.main()
        mock_top.assert_called_once()

    @patch("pipeline.social.reddit_scrape.fetch_post_with_comments")
    @patch("pipeline.social.reddit_scrape.fetch_top_post")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.mkdir")
    def test_main_with_corrupt_skip_ids_file(self, mock_mkdir, mock_write, mock_top, mock_comments):
        """Corrupt skip-ids file should be silently ignored."""
        mock_top.return_value = _good_post()
        mock_comments.return_value = self._bundle()
        with patch("sys.argv", [
            "prog", "--subreddit", "AmItheAsshole", "--out", "_test_out.json",
            "--skip-ids-file", "fake_skip.json",
        ]):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value="not valid json {{{"):
                    reddit_scrape.main()
        # Should still proceed without crashing
        mock_top.assert_called_once()


if __name__ == "__main__":
    unittest.main()
