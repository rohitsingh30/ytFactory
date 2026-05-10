"""Tests for control/routes/discover_routes.py — 100% line coverage."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")
# Default to LLM disabled in tests — tests that exercise the LLM
# brainstorm path opt back in via patch("..._llm_disabled", ...).
os.environ.setdefault("YTFACTORY_DISCOVER_LLM_DISABLE", "1")

import httpx
from fastapi import FastAPI

from control.routes.discover_routes import (
    DiscoverFeed,
    DiscoverItem,
    DiscoverRequest,
    _ai_news_items,
    _build_feed,
    _excerpt,
    _filter_avoid,
    _llm_topic_items,
    _native_items_for,
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


class TestNativeItemsFor(unittest.TestCase):
    def test_mystoriesanimated_default_variant_routes_to_aita(self) -> None:
        stories = [_raw_story()]
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories) as f:
            label, items = _native_items_for("mystoriesanimated", DiscoverRequest(), limit=4)
        self.assertIn("AmItheAsshole", label)
        self.assertEqual(len(items), 1)
        # Subreddit threaded into reddit_api.fetch
        self.assertEqual(f.call_args.kwargs["subreddit"], "AmItheAsshole")

    def test_mystoriesanimated_tifu_variant_routes_to_tifu_subreddit(self) -> None:
        stories = [_raw_story("TIFU by reading code", "I read it for hours. " * 5)]
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories) as f:
            label, items = _native_items_for(
                "mystoriesanimated", DiscoverRequest(variant="tifu"), limit=4,
            )
        self.assertEqual(f.call_args.kwargs["subreddit"], "tifu")
        self.assertIn("tifu", label)
        self.assertTrue(items[0].topic.startswith("TIFU"))

    def test_mystoriesanimated_malicious_variant_routes_to_malicious_compliance(self) -> None:
        with patch("pipeline.sources.reddit_api.fetch", return_value=[_raw_story()]) as f:
            _native_items_for(
                "mystoriesanimated", DiscoverRequest(variant="malicious"), limit=4,
            )
        self.assertEqual(f.call_args.kwargs["subreddit"], "MaliciousCompliance")

    def test_mystoriesanimated_oddities_variant_routes_to_wikipedia(self) -> None:
        with patch("pipeline.sources.today_in_history.fetch",
                   return_value=[_raw_story("Oddity", "Strange thing happened. " * 5)]):
            label, items = _native_items_for(
                "mystoriesanimated", DiscoverRequest(variant="oddities"), limit=4,
            )
        self.assertIn("wikipedia", label)
        self.assertEqual(items[0].source_kind, "wikipedia_topic")

    def test_no_native_channel_returns_empty(self) -> None:
        for ch in ("hindutavaanimated", "sportsrecapped", "rhymetimejunction"):
            label, items = _native_items_for(ch, DiscoverRequest(), limit=4)
            self.assertEqual(label, "")
            self.assertEqual(items, [])


class TestFilterAvoid(unittest.TestCase):
    def test_empty_avoid_passthrough(self) -> None:
        items = [DiscoverItem(topic="A", source_kind="llm", source_label="x")]
        self.assertEqual(_filter_avoid(items, []), items)

    def test_blocks_by_topic(self) -> None:
        items = [
            DiscoverItem(topic="Keep me", source_kind="llm", source_label="x"),
            DiscoverItem(topic="Drop me", source_kind="llm", source_label="x"),
        ]
        out = _filter_avoid(items, ["drop me"])  # case-insensitive
        self.assertEqual([it.topic for it in out], ["Keep me"])

    def test_blocks_by_source_ref(self) -> None:
        items = [
            DiscoverItem(topic="A", source_kind="reddit_url",
                         source_ref="https://r/x/1", source_label="x"),
            DiscoverItem(topic="B", source_kind="reddit_url",
                         source_ref="https://r/x/2", source_label="x"),
        ]
        out = _filter_avoid(items, ["https://r/x/1"])
        self.assertEqual([it.topic for it in out], ["B"])


class TestLlmTopicItems(unittest.TestCase):
    def test_disabled_via_env(self) -> None:
        with patch("control.routes.discover_routes._llm_disabled", return_value=True):
            items = _llm_topic_items("hindutavaanimated", DiscoverRequest())
        self.assertEqual(items, [])

    def test_returns_parsed_items(self) -> None:
        result = {"items": [
            {"topic": "Krishna Sudama", "hook": "What if your best friend was God?"},
            {"topic": "Karna's last arrow"},
        ]}
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=result) as call:
            items = _llm_topic_items(
                "hindutavaanimated",
                DiscoverRequest(variant="mahabharat", language="hi"),
                count=4,
            )
        self.assertGreaterEqual(len(items), 2)
        self.assertEqual(items[0].source_kind, "llm")
        self.assertEqual(items[0].topic, "Krishna Sudama")
        self.assertIn("hindutavaanimated", call.call_args.args[0].lower())
        # Hook is preserved in metadata
        self.assertEqual(items[0].metadata["hook"], "What if your best friend was God?")

    def test_handles_string_payload(self) -> None:
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm",
                   return_value='{"items":[{"topic":"From string"}]}'):
            items = _llm_topic_items("sportsrecapped", DiscoverRequest(), count=3)
        self.assertEqual(items[0].topic, "From string")

    def test_returns_empty_on_llm_failure(self) -> None:
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", side_effect=RuntimeError("network down")):
            items = _llm_topic_items("hindutavaanimated", DiscoverRequest())
        self.assertEqual(items, [])

    def test_dedupes_items(self) -> None:
        result = {"items": [
            {"topic": "Same"}, {"topic": "Same"}, {"topic": "Other"},
        ]}
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=result):
            items = _llm_topic_items("rhymetimejunction", DiscoverRequest(), count=5)
        self.assertEqual([it.topic for it in items], ["Same", "Other"])


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

    def test_unknown_channel_with_no_native_and_no_llm_raises_502(self) -> None:
        """Channels with no native adapter AND LLM disabled → 502 (not 422)."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _build_feed("hindutavaanimated")  # LLM disabled by env in this test module
        self.assertEqual(ctx.exception.status_code, 502)

    def test_unknown_channel_with_llm_succeeds(self) -> None:
        """No native adapter + LLM available → returns LLM-only feed."""
        result = {"items": [{"topic": "Hanuman & Sanjeevani"}]}
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=result):
            feed = _build_feed("hindutavaanimated", req=DiscoverRequest(variant="ramayan"))
        self.assertGreaterEqual(len(feed.items), 1)
        self.assertIn("llm", feed.adapter)
        self.assertEqual(feed.items[0].source_kind, "llm")

    def test_llm_blended_with_native(self) -> None:
        """Both native + LLM produce items → adapter shows both."""
        stories = [_raw_story("AITA story", "I did something. " * 10)]
        result = {"items": [{"topic": "Brainstormed angle"}]}
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories), \
             patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=result):
            feed = _build_feed("mystoriesanimated", req=DiscoverRequest())
        # Adapter shows both sources
        self.assertIn("reddit", feed.adapter)
        self.assertIn("llm", feed.adapter)
        # Both kinds present in items
        kinds = {it.source_kind for it in feed.items}
        self.assertIn("reddit_url", kinds)
        self.assertIn("llm", kinds)

    def test_avoid_filters_native_items(self) -> None:
        stories = [
            _raw_story("Keep this", "x. " * 20, url="https://r/x/keep"),
            _raw_story("Skip this", "x. " * 20, url="https://r/x/skip"),
        ]
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories):
            feed = _build_feed("mystoriesanimated", req=DiscoverRequest(avoid=["Skip this"]))
        topics = [it.topic for it in feed.items]
        self.assertIn("Keep this", topics)
        self.assertNotIn("Skip this", topics)

    def test_native_source_failure_falls_back_to_llm(self) -> None:
        """Reddit blowing up shouldn't 502 the request — LLM picks up the slack.

        Regression for the prod 502 spam where a transient Reddit hiccup
        on /api/discover/mystoriesanimated took the auto-generate button
        out for everyone. _safe_native_items_for now swallows the
        exception so the LLM brainstorm carries the response.
        """
        result = {"items": [{"topic": "Brainstormed angle"}]}
        with patch("pipeline.sources.reddit_api.fetch",
                   side_effect=RuntimeError("reddit 429")), \
             patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=result):
            feed = _build_feed("mystoriesanimated", req=DiscoverRequest())
        self.assertGreaterEqual(len(feed.items), 1)
        self.assertEqual(feed.items[0].source_kind, "llm")
        # Adapter no longer mentions the (failed) reddit source.
        self.assertNotIn("reddit", feed.adapter)


class TestDiscoverRequestCoercion(unittest.TestCase):
    """Hard 422s for routine FE drift were the second-biggest source of
    /api/discover spam. The validators below keep the contract loose
    where the consequences are cosmetic."""

    def test_null_values_dict_becomes_empty(self) -> None:
        req = DiscoverRequest.model_validate({"values": None})
        self.assertEqual(req.values, {})

    def test_avoid_drops_non_string_entries(self) -> None:
        req = DiscoverRequest.model_validate({"avoid": [None, "real-topic", 5, True]})
        self.assertEqual(req.avoid, ["real-topic", "5"])

    def test_avoid_string_wrapped_into_list(self) -> None:
        req = DiscoverRequest.model_validate({"avoid": "single-string"})
        self.assertEqual(req.avoid, ["single-string"])

    def test_optional_str_fields_strip_and_nullify_blanks(self) -> None:
        req = DiscoverRequest.model_validate({
            "variant": "  ",
            "length_kind": "  long  ",
            "language": "",
            "niche_key": None,
        })
        self.assertIsNone(req.variant)
        self.assertEqual(req.length_kind, "long")
        self.assertIsNone(req.language)
        self.assertIsNone(req.niche_key)

    def test_non_string_optional_field_coerced(self) -> None:
        # Ints / floats are stringified (helpful when the FE accidentally
        # forwards a numeric value); bools are silently dropped because
        # "True" / "False" is never a valid variant / language / etc.
        req = DiscoverRequest.model_validate({"variant": 1, "length_kind": True})
        self.assertEqual(req.variant, "1")
        self.assertIsNone(req.length_kind)

    def test_non_dict_values_dropped_silently(self) -> None:
        req = DiscoverRequest.model_validate({"values": "broken"})
        self.assertEqual(req.values, {})


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

    async def test_feed_no_native_no_llm_502(self) -> None:
        """Channels with no native adapter return 502 when LLM is disabled."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/discover/hindutavaanimated/feed")
        self.assertEqual(r.status_code, 502)

    async def test_feed_with_variant_query(self) -> None:
        """Passing variant=tifu in query string routes to r/tifu."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("pipeline.sources.reddit_api.fetch",
                   return_value=[_raw_story("TIFU", "x. " * 20)]) as f:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/discover/mystoriesanimated/feed?variant=tifu")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(f.call_args.kwargs["subreddit"], "tifu")

    async def test_feed_upstream_error_502(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("pipeline.sources.reddit_api.fetch", side_effect=RuntimeError("network error")):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/discover/mystoriesanimated/feed")
        self.assertEqual(r.status_code, 502)

    async def test_feed_native_failure_falls_back_to_llm(self) -> None:
        """Production regression: Reddit blocks Cloud Run egress IPs with
        a 403, but the LLM brainstorm should still carry the response.
        Pre-fix this 502'd the whole route."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        result = {"items": [{"topic": "Drafted by LLM when Reddit was 403"}]}
        with patch("pipeline.sources.reddit_api.fetch",
                   side_effect=RuntimeError("403 Blocked")), \
             patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=result):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/api/discover/mystoriesanimated/feed?variant=tifu",
                )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        # Adapter should signal the native failure but still surface LLM
        self.assertIn("llm", data["adapter"])
        self.assertGreaterEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["source_kind"], "llm")


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

    async def test_pick_one_with_context_body(self) -> None:
        """POST body carries variant/values/avoid; backend honours them."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        stories = [
            _raw_story("Avoid me", "x. " * 20, url="https://r/x/1"),
            _raw_story("Pick me", "x. " * 20, url="https://r/x/2"),
        ]
        body = {"variant": "tifu", "length_kind": "short",
                "language": "en", "avoid": ["Avoid me"]}
        with patch("pipeline.sources.reddit_api.fetch", return_value=stories) as f:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/discover/mystoriesanimated", json=body)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["topic"], "Pick me")
        self.assertEqual(f.call_args.kwargs["subreddit"], "tifu")

    async def test_pick_one_hindutava_with_llm(self) -> None:
        """Channel with no native adapter — pick_one succeeds via LLM."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        result = {"items": [{"topic": "Bhima vs Jarasandha"}]}
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=result):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/discover/hindutavaanimated",
                                      json={"variant": "mahabharat", "language": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["topic"], "Bhima vs Jarasandha")


if __name__ == "__main__":
    unittest.main()
