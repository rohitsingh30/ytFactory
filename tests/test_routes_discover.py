"""Tests for control/routes/discover_routes.py — 100% line coverage."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import httpx
from fastapi import FastAPI

from control.routes.discover_routes import (
    DiscoverFeed,
    DiscoverItem,
    _ai_news_items,
    _build_feed,
    _excerpt,
    _reddit_items,
    _today_in_history_items,
    router,
)
from pipeline.sources.reddit_api import RawStory


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _raw_story(title: str = "Test Title", body: str = "Test body text",
               url: str = "https://reddit.com/r/test/1") -> RawStory:
    return RawStory(
        slug="test-slug",
        title=title,
        body=body,
        source="reddit:test",
        url=url,
        metadata={"score": 100},
    )


class TestExcerpt(unittest.TestCase):
    def test_short_text(self) -> None:
        result = _excerpt("Hello world", n=280)
        self.assertEqual(result, "Hello world")

    def test_long_text_truncated(self) -> None:
        text = "x" * 300
        result = _excerpt(text, n=280)
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(len(result), 280)

    def test_newlines_replaced(self) -> None:
        result = _excerpt("line1\n\nline2")
        self.assertIn("¶", result)

    def test_carriage_return_stripped(self) -> None:
        result = _excerpt("line1\r\nline2")
        self.assertNotIn("\r", result)

    def test_empty_string(self) -> None:
        result = _excerpt("")
        self.assertEqual(result, "")


class TestRedditItems(unittest.TestCase):
    def test_returns_items(self) -> None:
        stories = [_raw_story("AITA for testing?", "I wrote a test. " * 20)]
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories):
            items = _reddit_items("AmItheAsshole")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].topic, "AITA for testing?")
        self.assertEqual(items[0].source_kind, "reddit_url")

    def test_empty_result(self) -> None:
        with patch("pipeline.sources.reddit_api.fetch", return_value=[]):
            items = _reddit_items("AmItheAsshole")
        self.assertEqual(items, [])


class TestTodayInHistoryItems(unittest.TestCase):
    def test_returns_items(self) -> None:
        stories = [_raw_story("Moon landing", "Neil Armstrong landed. " * 10, "https://en.wikipedia.org/wiki/Moon")]
        with patch("pipeline.sources.today_in_history.fetch", return_value=stories):
            items = _today_in_history_items(limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_kind, "wikipedia_topic")

    def test_keyword_filter(self) -> None:
        stories = [
            _raw_story("Space launch", "A rocket went to space. " * 5),
            _raw_story("Cooking show", "Someone cooked pasta. " * 5),
        ]
        with patch("pipeline.sources.today_in_history.fetch", return_value=stories):
            items = _today_in_history_items(limit=5, keyword="space")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].topic, "Space launch")

    def test_keyword_in_body(self) -> None:
        stories = [_raw_story("Regular Title", "The body mentions space exploration.")]
        with patch("pipeline.sources.today_in_history.fetch", return_value=stories):
            items = _today_in_history_items(limit=5, keyword="space")
        self.assertEqual(len(items), 1)

    def test_limit_respected(self) -> None:
        stories = [_raw_story(f"Event {i}", f"Something happened. " * 5) for i in range(10)]
        with patch("pipeline.sources.today_in_history.fetch", return_value=stories):
            items = _today_in_history_items(limit=3)
        self.assertLessEqual(len(items), 3)

    def test_metadata_year(self) -> None:
        story = _raw_story("Apollo 11", "Moon landing. " * 10)
        story.metadata["year"] = 1969
        with patch("pipeline.sources.today_in_history.fetch", return_value=[story]):
            items = _today_in_history_items(limit=5)
        self.assertEqual(items[0].metadata["year"], 1969)


class TestAiNewsItems(unittest.TestCase):
    def test_returns_items(self) -> None:
        stories = [_raw_story("AI breakthrough", "GPT is amazing. " * 10, "https://news.ycombinator.com/1")]
        with patch("pipeline.sources.ai_news.fetch", return_value=stories):
            items = _ai_news_items(limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_kind, "user_text")
        self.assertEqual(items[0].source_label, "HN · top AI today")

    def test_empty_result(self) -> None:
        with patch("pipeline.sources.ai_news.fetch", return_value=[]):
            items = _ai_news_items()
        self.assertEqual(items, [])


class TestBuildFeed(unittest.TestCase):
    def test_mystoriesanimated(self) -> None:
        stories = [_raw_story()]
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories):
            feed = _build_feed("mystoriesanimated")
        self.assertEqual(feed.channel, "mystoriesanimated")
        self.assertIn("AmItheAsshole", feed.adapter)

    def test_scrollpulse(self) -> None:
        stories = [_raw_story()]
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories):
            feed = _build_feed("scrollpulse")
        self.assertEqual(feed.channel, "scrollpulse")
        self.assertIn("round-robin", feed.adapter)

    def test_historyrecapped(self) -> None:
        stories = [_raw_story("Historical", "Long ago. " * 10)]
        with patch("pipeline.sources.today_in_history.fetch", return_value=stories):
            feed = _build_feed("historyrecapped")
        self.assertEqual(feed.channel, "historyrecapped")
        self.assertIn("wikipedia", feed.adapter)

    def test_cosmosdecoded(self) -> None:
        stories = [_raw_story("Space event", "A rocket launched into space. " * 5)]
        with patch("pipeline.sources.today_in_history.fetch", return_value=stories):
            feed = _build_feed("cosmosdecoded")
        self.assertEqual(feed.channel, "cosmosdecoded")
        self.assertIn("physics", feed.adapter)

    def test_cosmosdecoded_deduplication(self) -> None:
        """Same URL/topic shouldn't appear twice."""
        story = _raw_story("Space event", "A rocket launched into space. " * 5,
                            url="https://en.wikipedia.org/wiki/space")
        with patch("pipeline.sources.today_in_history.fetch", return_value=[story] * 5):
            feed = _build_feed("cosmosdecoded", limit=8)
        # Deduplicated — only one result
        refs = [it.source_ref for it in feed.items]
        self.assertEqual(len(refs), len(set(refs)))

    def test_unknown_channel_raises_422(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _build_feed("unknownchan")
        self.assertEqual(ctx.exception.status_code, 422)


class TestFeedEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_feed_ok(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        stories = [_raw_story()]
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/discover/mystoriesanimated/feed")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["channel"], "mystoriesanimated")
        self.assertIn("items", data)

    async def test_feed_unknown_channel_422(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/discover/unknownchan/feed")
        self.assertEqual(r.status_code, 422)

    async def test_feed_upstream_error_502(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("pipeline.sources.reddit_api.fetch", side_effect=RuntimeError("network error")):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/discover/mystoriesanimated/feed")
        self.assertEqual(r.status_code, 502)


class TestPickOne(unittest.IsolatedAsyncioTestCase):
    async def test_pick_one_success(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        stories = [_raw_story(f"Story {i}", f"Body text. " * 10) for i in range(3)]
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/discover/mystoriesanimated")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("topic", data)

    async def test_pick_one_empty_feed_502(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("pipeline.sources.reddit_api.fetch", return_value=[]):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/discover/mystoriesanimated")
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()
