"""Tests for pipeline.sources.reddit_api."""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import patch

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
        # Anon backend wraps requests.HTTPError in RedditFetchError so
        # callers can catch a single typed exception. The original
        # exception is preserved as ``__cause__``.
        from requests import HTTPError

        from pipeline.sources.reddit_api import RedditFetchError
        resp = _FakeResponse(status_code=429)
        with patch.object(reddit_api.requests, "get", return_value=resp):
            with self.assertRaises(RedditFetchError) as ctx:
                reddit_api.fetch("AmItheAsshole", backend="anon")
        self.assertIsInstance(ctx.exception.__cause__, HTTPError)

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


class PickBackendTest(unittest.TestCase):
    """Pin the precedence rules in ``_pick_backend()``.

    These rules are load-bearing: getting them wrong means cloud
    silently regresses to ``anon`` (403s) or laptop dev hits the slow
    Pullpush archive instead of live Reddit.
    """

    def setUp(self):
        # Snapshot + clear all relevant env vars so each test starts
        # from a clean slate regardless of the host shell.
        self._saved = {
            k: os.environ.get(k)
            for k in ("REDDIT_FETCH_BACKEND", "K_SERVICE",
                      "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_is_anon(self):
        self.assertEqual(reddit_api._pick_backend(), "anon")

    def test_k_service_picks_pullpush(self):
        os.environ["K_SERVICE"] = "ytfactory-web"
        self.assertEqual(reddit_api._pick_backend(), "pullpush")

    def test_explicit_env_wins_over_k_service(self):
        os.environ["K_SERVICE"] = "ytfactory-web"
        os.environ["REDDIT_FETCH_BACKEND"] = "anon"
        self.assertEqual(reddit_api._pick_backend(), "anon")

    def test_explicit_env_is_case_insensitive(self):
        os.environ["REDDIT_FETCH_BACKEND"] = "PullPush"
        self.assertEqual(reddit_api._pick_backend(), "pullpush")

    def test_unknown_explicit_env_falls_through_to_default(self):
        # A typo'd value mustn't silently coerce — fall back to the
        # auto-pick path so the failure mode is the same as "env unset".
        os.environ["REDDIT_FETCH_BACKEND"] = "random_garbage"
        self.assertEqual(reddit_api._pick_backend(), "anon")
        os.environ["K_SERVICE"] = "ytfactory-web"
        self.assertEqual(reddit_api._pick_backend(), "pullpush")

    def test_oauth_creds_alone_do_NOT_auto_pick_oauth(self):
        # Footgun guard: until OAuth is actually implemented, having
        # REDDIT_CLIENT_ID + _SECRET set must NOT activate the broken
        # path. Cloud must stay on Pullpush.
        os.environ["K_SERVICE"] = "ytfactory-web"
        os.environ["REDDIT_CLIENT_ID"] = "x"
        os.environ["REDDIT_CLIENT_SECRET"] = "y"
        self.assertEqual(reddit_api._pick_backend(), "pullpush")

    def test_explicit_oauth_picks_oauth_even_though_unimplemented(self):
        # The dispatcher honours the explicit env so the operator gets
        # a clear NotImplementedError pointing them at the doc, rather
        # than a silent degrade to anon/pullpush.
        os.environ["REDDIT_FETCH_BACKEND"] = "oauth"
        self.assertEqual(reddit_api._pick_backend(), "oauth")


class OAuthBackendTest(unittest.TestCase):
    """The OAuth backend is documented but not yet implemented.

    Both entry points must raise NotImplementedError with a message
    that names the alternative (``pullpush`` / ``anon``) so an operator
    who hits this in cloud logs knows what to set.
    """

    def test_fetch_oauth_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError) as ctx:
            reddit_api.fetch("AmItheAsshole", backend="oauth")
        self.assertIn("REDDIT_FETCH_BACKEND=oauth", str(ctx.exception))
        self.assertIn("pullpush", str(ctx.exception).lower())

    def test_fetch_post_by_url_oauth_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError) as ctx:
            reddit_api.fetch_post_by_url(
                "https://reddit.com/r/AmItheAsshole/comments/abc/title/",
                backend="oauth",
            )
        self.assertIn("REDDIT_FETCH_BACKEND=oauth", str(ctx.exception))


class FetchViaPullpushTest(unittest.TestCase):
    """End-to-end behaviour of the Pullpush adapter."""

    def _pullpush_response(self, items):
        return _FakeResponse(json_payload={"data": items, "metadata": {}, "error": None})

    def _pullpush_item(self, *, title="Story", selftext=None, post_id="abc",
                        score=42, num_comments=7, author="u",
                        is_self=True, stickied=False, over_18=False,
                        permalink=None, subreddit="AmItheAsshole"):
        if selftext is None:
            selftext = "x" * 500
        if permalink is None:
            permalink = f"/r/{subreddit}/comments/{post_id}/{slug_for(title)}/"
        return {
            "title": title,
            "selftext": selftext,
            "permalink": permalink,
            "is_self": is_self,
            "stickied": stickied,
            "over_18": over_18,
            "score": score,
            "num_comments": num_comments,
            "author": author,
            "id": post_id,
            "created_utc": 1714694400,
            "subreddit": subreddit,
        }

    def test_happy_path_returns_raw_stories(self):
        items = [self._pullpush_item(title="Story A", post_id="aaa")]
        with patch.object(reddit_api.requests, "get",
                          return_value=self._pullpush_response(items)):
            out = reddit_api.fetch("AmItheAsshole", backend="pullpush", limit=5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "Story A")
        self.assertEqual(out[0].source, "reddit:AmItheAsshole")
        self.assertIn("reddit.com", out[0].url)
        self.assertEqual(out[0].metadata["backend"], "pullpush")
        # Window label is the human "24h" form; offset is the actual
        # API param.
        self.assertEqual(out[0].metadata["pullpush_window"], "24h")

    def test_removed_selftext_is_filtered(self):
        items = [
            self._pullpush_item(title="Removed", selftext="[removed]", post_id="r1"),
            self._pullpush_item(title="Deleted", selftext="[deleted]", post_id="r2"),
            self._pullpush_item(title="Real",    selftext="x" * 600,    post_id="r3"),
        ]
        with patch.object(reddit_api.requests, "get",
                          return_value=self._pullpush_response(items)):
            out = reddit_api.fetch("AmItheAsshole", backend="pullpush", limit=5)
        self.assertEqual([s.title for s in out], ["Real"])

    def test_calls_pullpush_url_with_expected_params(self):
        captured = {}

        def capture_get(url, *, params=None, **kwargs):
            captured.setdefault("calls", []).append((url, dict(params or {})))
            return self._pullpush_response([])

        with patch.object(reddit_api.requests, "get", side_effect=capture_get):
            reddit_api.fetch("AmItheAsshole", backend="pullpush",
                             listing="top", timeframe="day", limit=5)

        self.assertGreaterEqual(len(captured["calls"]), 1)
        url, params = captured["calls"][0]
        self.assertIn("api.pullpush.io", url)
        self.assertEqual(params["subreddit"], "AmItheAsshole")
        self.assertEqual(params["sort"], "desc")
        self.assertEqual(params["sort_type"], "score")
        # ``after`` must be a Unix-timestamp INTEGER as a string —
        # Pullpush returns 400 on the ``24h`` shortcut form. Verify
        # it parses to an int and is roughly "now minus 24h".
        self.assertIn("after", params)
        after_int = int(params["after"])
        now = int(time.time())
        # Allow ±5 minute drift for slow CI / leap-second nonsense.
        self.assertLess(abs((now - after_int) - 86_400), 300)
        # Overfetches hard — the [removed] filter is brutal.
        self.assertEqual(params["size"], "100")
        # Pullpush 400s on over_18=false, so we MUST NOT send it.
        # NSFW is filtered client-side instead.
        self.assertNotIn("over_18", params)

    def test_widens_window_when_strict_yields_too_few(self):
        # First call (24h): only [removed] / non-self → 0 usable.
        # Second call (7d):  one usable item — limit=1 satisfied, stop.
        empty = self._pullpush_response([
            self._pullpush_item(selftext="[removed]", post_id="a"),
            self._pullpush_item(selftext="[removed]", post_id="b"),
        ])
        good = self._pullpush_response([
            self._pullpush_item(title="From last week", post_id="c"),
        ])
        responses = [empty, good]

        def side_effect(url, **kwargs):
            return responses.pop(0)

        with patch.object(reddit_api.requests, "get", side_effect=side_effect):
            out = reddit_api.fetch("AmItheAsshole", backend="pullpush",
                                   timeframe="day", limit=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "From last week")
        self.assertEqual(out[0].metadata["pullpush_window"], "7d")

    def test_widening_dedupes_by_post_id(self):
        # When the 7d window includes the 24h window's items (which it
        # naturally does), we mustn't surface the same post twice.
        # First call (24h) returns one usable item with post_id="x1".
        # Second call (7d) returns the SAME post_id plus a new one.
        twentyfour = self._pullpush_response([
            self._pullpush_item(title="Same post", post_id="x1"),
        ])
        sevenday = self._pullpush_response([
            self._pullpush_item(title="Same post",  post_id="x1"),
            self._pullpush_item(title="Different",  post_id="x2"),
        ])
        empty_response = self._pullpush_response([])
        # Cascade keeps trying month/year/all because limit=5 never met.
        responses = [twentyfour, sevenday, empty_response, empty_response, empty_response]

        def side_effect(url, **kwargs):
            return responses.pop(0)

        with patch.object(reddit_api.requests, "get", side_effect=side_effect):
            out = reddit_api.fetch("AmItheAsshole", backend="pullpush",
                                   timeframe="day", limit=5)
        # Both windows ran (limit=5, only 1 from 24h, then 1 unique
        # from 7d). Final list has 2 distinct posts, not 3.
        self.assertEqual([s.title for s in out], ["Same post", "Different"])
        self.assertEqual(out[0].metadata["pullpush_window"], "24h")
        self.assertEqual(out[1].metadata["pullpush_window"], "7d")

    def test_widening_dedupes_window_when_timeframe_already_in_cascade(self):
        # When the caller asks for ``timeframe="week"``, the requested
        # window IS one of the cascade entries (7d, offset=604_800).
        # The dedup logic in ``_add()`` must skip the cascade's 7d
        # entry rather than querying it twice.
        sevenday = self._pullpush_response([
            self._pullpush_item(title="Week story", post_id="w1"),
        ])
        empty_response = self._pullpush_response([])
        # Cascade with week=requested: (7d, 30d, 365d, all) — four windows.
        responses = [sevenday, empty_response, empty_response, empty_response]
        captured_offsets: list[int | None] = []

        def side_effect(url, *, params=None, **kwargs):
            after = (params or {}).get("after")
            if after is None:
                captured_offsets.append(None)
            else:
                captured_offsets.append(int(time.time()) - int(after))
            return responses.pop(0)

        with patch.object(reddit_api.requests, "get", side_effect=side_effect):
            reddit_api.fetch("AmItheAsshole", backend="pullpush",
                             timeframe="week", limit=5)
        # 7d (~604_800s) MUST appear exactly once, not twice.
        seven_day_calls = sum(
            1 for o in captured_offsets
            if o is not None and abs(o - 604_800) < 5
        )
        self.assertEqual(seven_day_calls, 1)
        # ``all`` cascade tail still happens (None == no after param).
        self.assertIn(None, captured_offsets)

    def test_inner_break_when_limit_reached_mid_window(self):
        # The first 24h window already has more items than ``limit``
        # — the inner loop must break BEFORE iterating the rest, AND
        # the cascade must not advance to 7d at all.
        items = [self._pullpush_item(title=f"S{i}", post_id=f"p{i}")
                 for i in range(10)]
        responses = [self._pullpush_response(items)]
        # If a 2nd response is requested the test will fail loudly
        # — that's the regression we want to pin.

        def side_effect(url, **kwargs):
            return responses.pop(0)

        with patch.object(reddit_api.requests, "get", side_effect=side_effect):
            out = reddit_api.fetch("AmItheAsshole", backend="pullpush",
                                   timeframe="day", limit=3)
        self.assertEqual(len(out), 3)
        # Nothing left in the responses queue → no second call happened.
        self.assertEqual(responses, [])

    def test_all_window_omits_after_param(self):
        # The ``timeframe="all"`` cascade tail must drop the ``after``
        # param entirely (Pullpush returns top-of-all-time when omitted).
        captured_params: list[dict[str, str]] = []

        def side_effect(url, *, params=None, **kwargs):
            captured_params.append(dict(params or {}))
            return self._pullpush_response([])

        with patch.object(reddit_api.requests, "get", side_effect=side_effect):
            reddit_api.fetch("AmItheAsshole", backend="pullpush",
                             timeframe="all", limit=1)
        # Only one call (no widen — "all" is already the broadest).
        self.assertEqual(len(captured_params), 1)
        self.assertNotIn("after", captured_params[0])

    def test_top_maps_to_score_other_listings_to_created_utc(self):
        captured = []

        def capture_get(url, *, params=None, **kwargs):
            captured.append(dict(params or {}))
            return self._pullpush_response([])

        with patch.object(reddit_api.requests, "get", side_effect=capture_get):
            reddit_api.fetch("X", backend="pullpush", listing="top",  limit=1)
            captured.clear()
            reddit_api.fetch("X", backend="pullpush", listing="hot",  limit=1)
        # First "hot" call after the clear:
        self.assertEqual(captured[0]["sort_type"], "created_utc")

    def test_caller_specified_backend_overrides_pick_backend(self):
        # Even if K_SERVICE is set (i.e. _pick_backend would say
        # pullpush), an explicit ``backend="anon"`` kwarg forces anon —
        # used by tests to pin behaviour deterministically.
        items = [self._pullpush_item(post_id="z1")]
        anon_payload = {"data": {"children": [{"data": items[0]}]}}
        os.environ["K_SERVICE"] = "ytfactory-web"
        try:
            with patch.object(reddit_api.requests, "get",
                              return_value=_FakeResponse(json_payload=anon_payload)):
                out = reddit_api.fetch("X", backend="anon", limit=1)
        finally:
            os.environ.pop("K_SERVICE", None)
        self.assertEqual(len(out), 1)


class FetchPostByUrlTest(unittest.TestCase):
    """``fetch_post_by_url`` for the paste-a-Reddit-URL render flow."""

    POST_URL = ("https://www.reddit.com/r/AmItheAsshole/comments/"
                "1kqb0if/aita_for_cutting_my_niece_off/")

    def test_extract_post_id_from_full_url(self):
        self.assertEqual(reddit_api._extract_post_id(self.POST_URL), "1kqb0if")

    def test_extract_post_id_from_bare_permalink(self):
        permalink = "/r/AmItheAsshole/comments/1kqb0if/title/"
        self.assertEqual(reddit_api._extract_post_id(permalink), "1kqb0if")

    def test_extract_post_id_from_old_reddit_subdomain(self):
        url = "https://old.reddit.com/r/X/comments/abcd1/foo/"
        self.assertEqual(reddit_api._extract_post_id(url), "abcd1")

    def test_extract_post_id_returns_none_on_garbage(self):
        self.assertIsNone(reddit_api._extract_post_id("not-a-reddit-url"))
        self.assertIsNone(reddit_api._extract_post_id(""))

    def test_anon_post_fetch_returns_render_payload(self):
        # Anon endpoint returns a 2-element list: [submission, comments].
        anon_payload = [
            {"data": {"children": [{"data": {
                "title": "AITA",
                "selftext": "Long story body…",
                "permalink": "/r/AmItheAsshole/comments/1kqb0if/aita/",
                "subreddit": "AmItheAsshole",
                "is_self": True,
            }}]}},
            {"data": {"children": []}},  # comments — ignored
        ]
        with patch.object(reddit_api.requests, "get",
                          return_value=_FakeResponse(json_payload=anon_payload)):
            out = reddit_api.fetch_post_by_url(self.POST_URL, backend="anon")
        self.assertEqual(out["title"], "AITA")
        self.assertEqual(out["body"], "Long story body…")
        self.assertEqual(out["source"], "reddit:AmItheAsshole")
        self.assertIn("reddit.com", out["url"])

    def test_pullpush_post_fetch_returns_render_payload(self):
        # Primary ``?ids=`` lookup hits and returns the post.
        pp_payload = {"data": [{
            "title": "AITA",
            "selftext": "Long story body…",
            "permalink": "/r/AmItheAsshole/comments/1kqb0if/aita/",
            "subreddit": "AmItheAsshole",
            "id": "1kqb0if",
        }], "metadata": {}, "error": None}
        with patch.object(reddit_api.requests, "get",
                          return_value=_FakeResponse(json_payload=pp_payload)):
            out = reddit_api.fetch_post_by_url(self.POST_URL, backend="pullpush")
        self.assertEqual(out["title"], "AITA")
        self.assertEqual(out["body"], "Long story body…")
        self.assertEqual(out["source"], "reddit:AmItheAsshole")

    def test_pullpush_post_falls_back_to_slug_search_on_empty_id_lookup(self):
        # First call (``?ids=``) returns 0 — Pullpush's id lookup is
        # unreliable. Second call (``?subreddit=&q=slug as keywords``)
        # finds the post. The dispatcher must try the fallback before
        # raising.
        empty = _FakeResponse(json_payload={"data": [], "metadata": {}, "error": None})
        slug_match = _FakeResponse(json_payload={"data": [{
            "title": "AITA For Cutting My Niece Off",
            "selftext": "Long story body…",
            "permalink": "/r/AmItheAsshole/comments/1kqb0if/aita_for_cutting_my_niece_off/",
            "subreddit": "AmItheAsshole",
            "id": "1kqb0if",
        }], "metadata": {}, "error": None})

        captured_calls: list[dict[str, str]] = []

        def side_effect(url, *, params=None, **kwargs):
            captured_calls.append(dict(params or {}))
            return empty if "ids" in (params or {}) else slug_match

        with patch.object(reddit_api.requests, "get", side_effect=side_effect):
            out = reddit_api.fetch_post_by_url(self.POST_URL, backend="pullpush")
        self.assertEqual(out["title"], "AITA For Cutting My Niece Off")
        # Verify both lookups happened.
        self.assertEqual(len(captured_calls), 2)
        self.assertIn("ids", captured_calls[0])
        self.assertIn("q", captured_calls[1])
        self.assertEqual(captured_calls[1]["subreddit"], "AmItheAsshole")
        # Slug "aita_for_cutting_my_niece_off" → "aita for cutting my niece off"
        self.assertEqual(captured_calls[1]["q"], "aita for cutting my niece off")

    def test_pullpush_post_prefers_exact_id_match_in_slug_fallback(self):
        # Slug fallback can return multiple posts (same title, reposted).
        # When one of them matches the original post_id exactly, that's
        # the "real" post — pick it over a different post_id with the
        # same title.
        empty = _FakeResponse(json_payload={"data": [], "metadata": {}, "error": None})
        slug_match = _FakeResponse(json_payload={"data": [
            {  # repost — different post_id
                "title": "AITA For ...", "selftext": "different body",
                "permalink": "/r/AmItheAsshole/comments/zzz/aita_for_/",
                "subreddit": "AmItheAsshole", "id": "zzz",
            },
            {  # original post
                "title": "AITA For ...", "selftext": "original body",
                "permalink": "/r/AmItheAsshole/comments/1kqb0if/aita_for_/",
                "subreddit": "AmItheAsshole", "id": "1kqb0if",
            },
        ], "metadata": {}, "error": None})

        responses = [empty, slug_match]

        def side_effect(url, **kwargs):
            return responses.pop(0)

        with patch.object(reddit_api.requests, "get", side_effect=side_effect):
            out = reddit_api.fetch_post_by_url(self.POST_URL, backend="pullpush")
        self.assertEqual(out["body"], "original body")

    def test_pullpush_post_not_yet_archived_raises(self):
        # Both ``?ids=`` AND slug-fallback return empty → archive miss.
        # Caller catches RedditFetchError and degrades to user notes.
        from pipeline.sources.reddit_api import RedditFetchError
        empty = _FakeResponse(json_payload={"data": [], "metadata": {}, "error": None})
        with patch.object(reddit_api.requests, "get", return_value=empty):
            with self.assertRaises(RedditFetchError):
                reddit_api.fetch_post_by_url(self.POST_URL, backend="pullpush")

    def test_pullpush_post_no_slug_in_url_skips_fallback(self):
        # When the URL has no slug (``/r/X/comments/POSTID``), the
        # keyword-search fallback has no keywords to use — must skip
        # rather than hit Pullpush with an empty ``q`` param.
        from pipeline.sources.reddit_api import RedditFetchError
        empty = _FakeResponse(json_payload={"data": [], "metadata": {}, "error": None})
        captured = []

        def side_effect(url, *, params=None, **kwargs):
            captured.append(dict(params or {}))
            return empty

        bare_url = "https://www.reddit.com/r/AmItheAsshole/comments/1kqb0if"
        with patch.object(reddit_api.requests, "get", side_effect=side_effect):
            with self.assertRaises(RedditFetchError):
                reddit_api.fetch_post_by_url(bare_url, backend="pullpush")
        # Only the ``?ids=`` lookup happened; no slug fallback call.
        self.assertEqual(len(captured), 1)
        self.assertIn("ids", captured[0])

    def test_pullpush_post_with_removed_selftext_returns_empty_body(self):
        # Both ``?ids=`` returns the removed post directly.
        pp_payload = {"data": [{
            "title": "AITA original title preserved",
            "selftext": "[removed]",
            "permalink": "/r/AmItheAsshole/comments/1kqb0if/aita/",
            "subreddit": "AmItheAsshole",
            "id": "1kqb0if",
        }], "metadata": {}, "error": None}
        with patch.object(reddit_api.requests, "get",
                          return_value=_FakeResponse(json_payload=pp_payload)):
            out = reddit_api.fetch_post_by_url(self.POST_URL, backend="pullpush")
        # Title preserved (caller can show it as topic), body empty
        # (caller will fill from user notes).
        self.assertEqual(out["title"], "AITA original title preserved")
        self.assertEqual(out["body"], "")

    def test_garbage_url_raises_redditfetcherror(self):
        from pipeline.sources.reddit_api import RedditFetchError
        with self.assertRaises(RedditFetchError):
            reddit_api.fetch_post_by_url("https://example.com/not-a-reddit-url")

    def test_http_error_during_post_fetch_is_wrapped(self):
        # Pullpush returning a 5xx must surface as RedditFetchError so
        # the cloud render worker's ``except RedditFetchError`` clause
        # cleanly degrades to user-typed topic/notes.
        from requests import HTTPError

        from pipeline.sources.reddit_api import RedditFetchError
        with patch.object(reddit_api.requests, "get",
                          return_value=_FakeResponse(status_code=503)):
            with self.assertRaises(RedditFetchError) as ctx:
                reddit_api.fetch_post_by_url(self.POST_URL, backend="pullpush")
        self.assertIsInstance(ctx.exception.__cause__, HTTPError)


def slug_for(title):
    """Small helper used by ``FetchViaPullpushTest._pullpush_item`` to
    construct realistic-looking permalinks. Not the production
    slugifier — just enough to look like a real Reddit URL.
    """
    return "_".join(title.lower().split())


if __name__ == "__main__":
    unittest.main()
