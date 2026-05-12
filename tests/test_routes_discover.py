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
    _is_hn_rss,
    _llm_topic_items,
    _native_items_for,
    _niche_native_items_for,
    _reddit_items,
    _today_in_history_items,
    _wikipedia_list_items,
    router,
)
from pipeline.niche_specs import NicheDoc, NicheSource
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


# ---------------------------------------------------------------------------
# Niche-driven routing (2026-05-12 fix: discover must respect the user's
# selected niche, NOT the channel-default Wikipedia "On this day" feed).
# ---------------------------------------------------------------------------


def _niche(key: str, kind: str | None, ref: str | None = None,
           length_kind: str = "long") -> NicheDoc:
    """Build a NicheDoc for routing tests with the nested source shape
    that the GCS-persisted JSONs use."""
    return NicheDoc(
        key=key,
        label=key.replace("_", " ").title(),
        length_kind=length_kind,  # type: ignore[arg-type]
        source=NicheSource(kind=kind, ref=ref),
    )


class TestWikipediaListItems(unittest.TestCase):
    """``_wikipedia_list_items`` is the new adapter that scrapes a
    Wikipedia ``List_of_*`` page (delegating to
    :func:`pipeline.sources.wikipedia.fetch`) and emits per-entry
    DiscoverItems."""

    def _wiki_story(self, slug: str, title: str,
                    page: str = "List_of_wars") -> RawStory:
        return RawStory(
            slug=slug,
            title=title,
            body="b" * 200,
            source=f"wikipedia:{page}",
            url=f"https://en.wikipedia.org/wiki/{page}",
            metadata={"page": page, "license": "CC-BY-SA"},
        )

    def test_returns_per_entry_items(self) -> None:
        stories = [
            self._wiki_story("hundred-years-war", "Hundred Years' War"),
            self._wiki_story("ww1", "World War I"),
        ]
        with patch("pipeline.sources.wikipedia.fetch", return_value=stories) as f:
            items = _wikipedia_list_items(page="List_of_wars", limit=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].topic, "Hundred Years' War")
        self.assertEqual(items[0].source_kind, "wikipedia_topic")
        self.assertEqual(items[0].source_label, "Wikipedia · List of wars")
        self.assertEqual(f.call_args.kwargs["page"], "List_of_wars")
        self.assertEqual(f.call_args.kwargs["limit"], 5)

    def test_per_entry_source_ref_is_unique(self) -> None:
        """Critical for ``_filter_avoid`` not to nuke the entire page
        once one entry has been picked. The avoid list compares by
        ``source_ref``, so every entry must differ."""
        stories = [
            self._wiki_story("a", "Entry A"),
            self._wiki_story("b", "Entry B"),
            self._wiki_story("c", "Entry C"),
        ]
        with patch("pipeline.sources.wikipedia.fetch", return_value=stories):
            items = _wikipedia_list_items(page="List_of_wars", limit=5)
        refs = [it.source_ref for it in items]
        self.assertEqual(len(refs), len(set(refs)))
        # Each ref is the page URL plus the entry slug as a fragment.
        for it, st in zip(items, stories):
            self.assertEqual(it.source_ref, f"{st.url}#{st.slug}")
            self.assertEqual(it.metadata["page_url"], st.url)

    def test_hub_page_returning_no_entries(self) -> None:
        """Wikipedia hub pages (e.g. ``Lists_of_disasters``) often
        return nothing useful. The adapter must return ``[]`` so the
        caller can fall through to the LLM brainstorm."""
        with patch("pipeline.sources.wikipedia.fetch", return_value=[]):
            items = _wikipedia_list_items(page="Lists_of_disasters", limit=5)
        self.assertEqual(items, [])


class TestIsHnRss(unittest.TestCase):
    """RSS niches with an HN feed URL route to ``_ai_news_items``;
    every other RSS URL falls through to LLM-only."""

    def test_hn_canonical(self) -> None:
        self.assertTrue(_is_hn_rss("https://news.ycombinator.com/rss"))

    def test_hn_http_scheme(self) -> None:
        self.assertTrue(_is_hn_rss("http://news.ycombinator.com/rss"))

    def test_hn_uppercase_host(self) -> None:
        self.assertTrue(_is_hn_rss("https://NEWS.YCombinator.com/rss"))

    def test_other_feed_rejected(self) -> None:
        self.assertFalse(_is_hn_rss("https://feeds.bbci.co.uk/news/rss.xml"))

    def test_none_or_empty(self) -> None:
        self.assertFalse(_is_hn_rss(None))
        self.assertFalse(_is_hn_rss(""))

    def test_garbage_url_doesnt_raise(self) -> None:
        # urlparse swallows most malformed URLs; this is a defensive
        # belt-and-braces test for callers passing junk.
        self.assertFalse(_is_hn_rss("not a url at all"))


class TestNicheNativeItemsFor(unittest.TestCase):
    """Direct unit tests on ``_niche_native_items_for`` — the heart of
    the niche-driven dispatch table. One test per branch so the
    coverage gate pins every routing path."""

    def test_reddit_routes_to_subreddit(self) -> None:
        n = _niche("malicious", "reddit", "MaliciousCompliance")
        with patch("pipeline.sources.reddit_api.fetch",
                   return_value=[_raw_story("Top MC story", "x. " * 20)]) as f:
            label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual(label, "reddit:MaliciousCompliance")
        self.assertEqual(f.call_args.kwargs["subreddit"], "MaliciousCompliance")
        self.assertEqual(len(items), 1)

    def test_reddit_strips_leading_r_slash(self) -> None:
        """Defensive: hand-authored niches sometimes use 'r/Name'."""
        n = _niche("askh", "reddit", "r/AskHistorians")
        with patch("pipeline.sources.reddit_api.fetch",
                   return_value=[_raw_story("ask story", "x. " * 20)]) as f:
            label, _ = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual(label, "reddit:AskHistorians")
        self.assertEqual(f.call_args.kwargs["subreddit"], "AskHistorians")

    def test_reddit_with_null_ref_returns_empty(self) -> None:
        n = _niche("misconfigured", "reddit", None)
        label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual((label, items), ("", []))

    def test_reddit_with_only_r_slash_returns_empty(self) -> None:
        """Defensive: a hand-edited niche with ``ref="r/"`` (no actual
        sub) strips down to empty and must NOT call reddit_api with
        an empty subreddit."""
        for bad in ("r/", "/r/", "/", "//"):
            n = _niche("only_slash", "reddit", bad)
            with patch("pipeline.sources.reddit_api.fetch") as f:
                label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
            self.assertEqual((label, items), ("", []),
                             msg=f"ref={bad!r} should be treated as null")
            f.assert_not_called()

    def test_wikipedia_on_this_day_routes_to_today_in_history(self) -> None:
        n = _niche("history_today", "wikipedia", "On_this_day", length_kind="short")
        story = _raw_story("Apollo 11 landing", "Moon landing. " * 10)
        story.metadata["year"] = 1969
        with patch("pipeline.sources.today_in_history.fetch", return_value=[story]):
            label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual(label, "wikipedia:onthisday")
        self.assertEqual(items[0].source_kind, "wikipedia_topic")

    def test_wikipedia_list_page_routes_to_list_scraper(self) -> None:
        """The bug-fix path. ``ancient_civilizations`` niche on
        ``historyrecapped`` declares
        ``source: {kind: wikipedia, ref: List_of_ancient_civilizations}``
        and MUST hit the wiki list scraper, not today_in_history."""
        n = _niche("ancient_civilizations", "wikipedia",
                   "List_of_ancient_civilizations")
        story = RawStory(
            slug="indus-valley", title="Indus Valley Civilisation",
            body="The Indus Valley civilisation arose c. 3300 BCE. " * 4,
            source="wikipedia:List_of_ancient_civilizations",
            url="https://en.wikipedia.org/wiki/List_of_ancient_civilizations",
            metadata={"page": "List_of_ancient_civilizations"},
        )
        with patch("pipeline.sources.wikipedia.fetch", return_value=[story]) as f:
            label, items = _niche_native_items_for(n, DiscoverRequest(), limit=5)
        self.assertEqual(label, "wikipedia:List_of_ancient_civilizations")
        self.assertEqual(f.call_args.kwargs["page"],
                         "List_of_ancient_civilizations")
        self.assertEqual(items[0].topic, "Indus Valley Civilisation")
        self.assertEqual(items[0].source_kind, "wikipedia_topic")

    def test_wikipedia_with_null_ref_returns_empty(self) -> None:
        """Niches like ``historical_figures`` / ``wiki_physics`` declare
        ``wikipedia`` with no ref — they're "use the LLM with this
        niche label" niches, not native-source niches."""
        n = _niche("historical_figures", "wikipedia", None)
        label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual((label, items), ("", []))

    def test_rss_hn_url_routes_to_ai_news(self) -> None:
        n = _niche("ai_tech_daily", "rss", "https://news.ycombinator.com/rss")
        with patch("pipeline.sources.ai_news.fetch",
                   return_value=[_raw_story("HN top", "x. " * 20)]):
            label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual(label, "rss:news.ycombinator.com")
        self.assertEqual(items[0].source_kind, "user_text")

    def test_rss_unsupported_url_returns_empty(self) -> None:
        n = _niche("breaking_sports_news", "rss", "https://feeds.bbci.co.uk/sport/rss.xml")
        label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual((label, items), ("", []))

    def test_rss_null_ref_returns_empty(self) -> None:
        n = _niche("breaking_sports_news", "rss", None)
        label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual((label, items), ("", []))

    def test_manual_returns_empty(self) -> None:
        for ref in (None, "mahabharat"):
            n = _niche(f"m_{ref or 'none'}", "manual", ref)
            label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
            self.assertEqual((label, items), ("", []),
                             msg=f"manual ref={ref!r} should be LLM-only")

    def test_x_twitter_returns_empty(self) -> None:
        n = _niche("tweet_xfeed", "x_twitter", None)
        label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual((label, items), ("", []))

    def test_unknown_kind_returns_empty(self) -> None:
        """Forward-compat: a niche with an unknown source kind degrades
        to LLM-only rather than crashing."""
        n = _niche("future_kind", "tiktok_unreleased", "foo")
        label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual((label, items), ("", []))

    def test_missing_source_returns_empty(self) -> None:
        """A NicheDoc with no source field at all (legacy seed)."""
        n = NicheDoc(key="legacy", label="Legacy")
        # Wipe the auto-synthesised source so this exercises the
        # ``src is None`` branch.
        n.source = None
        label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual((label, items), ("", []))

    def test_source_with_null_kind_returns_empty(self) -> None:
        n = NicheDoc(key="nul", label="X", source=NicheSource(kind=None, ref=None))
        label, items = _niche_native_items_for(n, DiscoverRequest(), limit=4)
        self.assertEqual((label, items), ("", []))


class TestNativeItemsForNicheDriven(unittest.TestCase):
    """Tests the precedence wiring in ``_native_items_for``: niche
    selection ALWAYS wins over channel-default branches when set."""

    def test_history_ancient_civilizations_skips_today_in_history(self) -> None:
        """The exact user-reported bug: niche=ancient_civilizations on
        historyrecapped MUST scrape the wiki list page, NOT call
        today_in_history."""
        n = _niche("ancient_civilizations", "wikipedia",
                   "List_of_ancient_civilizations")
        story = RawStory(
            slug="rome", title="Roman Republic",
            body="Founded 509 BCE. " * 6,
            source="wikipedia:List_of_ancient_civilizations",
            url="https://en.wikipedia.org/wiki/List_of_ancient_civilizations",
            metadata={"page": "List_of_ancient_civilizations"},
        )
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n), \
             patch("pipeline.sources.wikipedia.fetch", return_value=[story]) as wiki, \
             patch("pipeline.sources.today_in_history.fetch") as tih:
            label, items = _native_items_for(
                "historyrecapped",
                DiscoverRequest(niche_key="ancient_civilizations"),
                limit=5,
            )
        self.assertEqual(label, "wikipedia:List_of_ancient_civilizations")
        self.assertEqual(items[0].topic, "Roman Republic")
        wiki.assert_called_once()
        # CRITICAL: today_in_history must NOT have been hit.
        tih.assert_not_called()

    def test_history_history_today_routes_to_today_in_history(self) -> None:
        n = _niche("history_today", "wikipedia", "On_this_day",
                   length_kind="short")
        story = _raw_story("Today event", "x. " * 20)
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n), \
             patch("pipeline.sources.today_in_history.fetch", return_value=[story]):
            label, items = _native_items_for(
                "historyrecapped",
                DiscoverRequest(niche_key="history_today"),
                limit=5,
            )
        self.assertEqual(label, "wikipedia:onthisday")
        self.assertEqual(len(items), 1)

    def test_history_askhistorians_routes_to_reddit(self) -> None:
        n = _niche("askhistorians", "reddit", "AskHistorians",
                   length_kind="short")
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n), \
             patch("pipeline.sources.reddit_api.fetch",
                   return_value=[_raw_story("ask story", "x. " * 20)]) as f:
            label, _ = _native_items_for(
                "historyrecapped",
                DiscoverRequest(niche_key="askhistorians"),
                limit=5,
            )
        self.assertEqual(label, "reddit:AskHistorians")
        self.assertEqual(f.call_args.kwargs["subreddit"], "AskHistorians")

    def test_history_history_quotes_manual_returns_empty_NOT_today_in_history(self) -> None:
        """Critical fallback semantics: a manual niche on historyrecapped
        MUST return empty native items so the LLM brainstorm carries.
        It must NOT silently fall back to channel-default
        today_in_history (the old bug)."""
        n = _niche("history_quotes", "manual", None, length_kind="short")
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n), \
             patch("pipeline.sources.today_in_history.fetch") as tih:
            label, items = _native_items_for(
                "historyrecapped",
                DiscoverRequest(niche_key="history_quotes"),
                limit=5,
            )
        self.assertEqual((label, items), ("", []))
        tih.assert_not_called()

    def test_history_no_niche_falls_back_to_channel_default(self) -> None:
        """Back-compat: no niche selected → legacy channel-default
        today_in_history. The GET feed endpoint without a query string
        relies on this."""
        story = _raw_story("Today event", "x. " * 20)
        with patch("pipeline.sources.today_in_history.fetch", return_value=[story]):
            label, items = _native_items_for(
                "historyrecapped",
                DiscoverRequest(),  # no niche_key
                limit=5,
            )
        self.assertEqual(label, "wikipedia:onthisday")
        self.assertEqual(len(items), 1)

    def test_history_unresolvable_niche_returns_empty(self) -> None:
        """niche_key set but lookup returns None (deleted niche, FE
        cache stale, typo). Must NOT silently fall back to channel
        default — same precedence rule as ``manual``."""
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=None), \
             patch("pipeline.sources.today_in_history.fetch") as tih:
            label, items = _native_items_for(
                "historyrecapped",
                DiscoverRequest(niche_key="not_a_real_niche"),
                limit=5,
            )
        self.assertEqual((label, items), ("", []))
        tih.assert_not_called()

    def test_cosmos_space_missions_routes_to_wiki_list(self) -> None:
        """Same fix on cosmosdecoded: a wiki-list niche must scrape
        that page, not the keyword-filtered today_in_history."""
        n = _niche("space_missions", "wikipedia", "List_of_space_missions")
        story = RawStory(
            slug="apollo-11", title="Apollo 11",
            body="x. " * 50, source="wikipedia:List_of_space_missions",
            url="https://en.wikipedia.org/wiki/List_of_space_missions",
            metadata={"page": "List_of_space_missions"},
        )
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n), \
             patch("pipeline.sources.wikipedia.fetch", return_value=[story]), \
             patch("pipeline.sources.today_in_history.fetch") as tih:
            label, items = _native_items_for(
                "cosmosdecoded",
                DiscoverRequest(niche_key="space_missions"),
                limit=5,
            )
        self.assertEqual(label, "wikipedia:List_of_space_missions")
        self.assertEqual(items[0].topic, "Apollo 11")
        tih.assert_not_called()

    def test_mystoriesanimated_malicious_niche_routes_to_reddit(self) -> None:
        n = _niche("malicious", "reddit", "MaliciousCompliance",
                   length_kind="short")
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n), \
             patch("pipeline.sources.reddit_api.fetch",
                   return_value=[_raw_story("MC story", "x. " * 20)]) as f:
            label, _ = _native_items_for(
                "mystoriesanimated",
                DiscoverRequest(niche_key="malicious"),
                limit=5,
            )
        self.assertEqual(label, "reddit:MaliciousCompliance")
        self.assertEqual(f.call_args.kwargs["subreddit"], "MaliciousCompliance")

    def test_mystoriesanimated_wiki_misconceptions_routes_to_list_page(self) -> None:
        """Behaviour change for the ``wiki_misconceptions`` variant:
        used to keyword-filter today_in_history; now (correctly)
        scrapes ``List_of_common_misconceptions``."""
        n = _niche("wiki_misconceptions", "wikipedia",
                   "List_of_common_misconceptions", length_kind="short")
        story = RawStory(
            slug="napoleon-height", title="Napoleon was not unusually short",
            body="x. " * 30, source="wikipedia:List_of_common_misconceptions",
            url="https://en.wikipedia.org/wiki/List_of_common_misconceptions",
            metadata={"page": "List_of_common_misconceptions"},
        )
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n), \
             patch("pipeline.sources.wikipedia.fetch", return_value=[story]), \
             patch("pipeline.sources.today_in_history.fetch") as tih:
            label, items = _native_items_for(
                "mystoriesanimated",
                DiscoverRequest(niche_key="wiki_misconceptions"),
                limit=5,
            )
        self.assertEqual(label, "wikipedia:List_of_common_misconceptions")
        self.assertEqual(items[0].topic,
                         "Napoleon was not unusually short")
        tih.assert_not_called()

    def test_no_native_channel_with_niche_returns_empty(self) -> None:
        """``hindutavaanimated`` has only manual niches — picking one
        still returns empty native items (LLM brainstorm carries)."""
        n = _niche("mahabharat", "manual", "mahabharat", length_kind="short")
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n):
            label, items = _native_items_for(
                "hindutavaanimated",
                DiscoverRequest(niche_key="mahabharat"),
                limit=5,
            )
        self.assertEqual((label, items), ("", []))


class TestBuildFeedNicheDriven(unittest.TestCase):
    """End-to-end: ``_build_feed`` must surface the niche-driven items
    AND continue to mix in LLM brainstorm. Pins the user-reported bug
    at the public API boundary."""

    def test_history_ancient_civilizations_does_not_show_today_in_history(self) -> None:
        """The exact bug regression. User picks niche
        ``ancient_civilizations``, none of the candidates should be a
        ``Wikipedia · on this day`` topic."""
        n = _niche("ancient_civilizations", "wikipedia",
                   "List_of_ancient_civilizations")
        wiki_story = RawStory(
            slug="indus-valley", title="Indus Valley Civilisation",
            body="x. " * 30, source="wikipedia:List_of_ancient_civilizations",
            url="https://en.wikipedia.org/wiki/List_of_ancient_civilizations",
            metadata={"page": "List_of_ancient_civilizations"},
        )
        # Even if today_in_history returned a Pope-assassination story,
        # the new path must never call it. Configure the mock so a
        # regression produces a clearly wrong topic in the assertion.
        bogus = _raw_story("On this day in 1982: Pope assassination attempt",
                           "x. " * 20)
        with patch("control.routes.discover_routes._resolve_niche_doc",
                   return_value=n), \
             patch("pipeline.sources.wikipedia.fetch", return_value=[wiki_story]), \
             patch("pipeline.sources.today_in_history.fetch", return_value=[bogus]):
            feed = _build_feed(
                "historyrecapped",
                req=DiscoverRequest(niche_key="ancient_civilizations"),
            )
        topics = [it.topic for it in feed.items]
        self.assertIn("Indus Valley Civilisation", topics)
        self.assertNotIn(
            "On this day in 1982: Pope assassination attempt", topics,
            msg="today_in_history must not pollute a niche-driven feed",
        )
        self.assertIn("wikipedia:List_of_ancient_civilizations", feed.adapter)


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

    def test_failure_path_logs_at_warning_level(self) -> None:
        # Exception during LLM call must surface at WARNING (not INFO)
        # so cloud log scans pick it up alongside the Reddit-403 line
        # when both legs of the discover feed are dead.
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", side_effect=RuntimeError("boom")):
            with self.assertLogs("control.routes.discover_routes", level="WARNING") as cm:
                items = _llm_topic_items("hindutavaanimated", DiscoverRequest())
        self.assertEqual(items, [])
        joined = "\n".join(cm.output)
        self.assertIn("LLM brainstorm raised", joined)
        self.assertIn("boom", joined)

    def test_non_json_string_logs_warning(self) -> None:
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value="not valid json {{{"):
            with self.assertLogs("control.routes.discover_routes", level="WARNING") as cm:
                items = _llm_topic_items("hindutavaanimated", DiscoverRequest())
        self.assertEqual(items, [])
        self.assertIn("non-JSON", "\n".join(cm.output))

    def test_non_dict_result_logs_warning(self) -> None:
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=["not", "a", "dict"]):
            with self.assertLogs("control.routes.discover_routes", level="WARNING") as cm:
                items = _llm_topic_items("hindutavaanimated", DiscoverRequest())
        self.assertEqual(items, [])
        self.assertIn("non-dict", "\n".join(cm.output))

    def test_items_field_not_a_list_logs_warning(self) -> None:
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm",
                   return_value={"items": "should-be-a-list"}):
            with self.assertLogs("control.routes.discover_routes", level="WARNING") as cm:
                items = _llm_topic_items("hindutavaanimated", DiscoverRequest())
        self.assertEqual(items, [])
        self.assertIn("not a list", "\n".join(cm.output))

    def test_empty_items_array_logs_warning(self) -> None:
        # The ACTUAL silent-failure mode that cost us hours on
        # 2026-05-12 — Azure honours response_format strictly and
        # returns ``{"items": []}`` rather than raising.
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value={"items": []}):
            with self.assertLogs("control.routes.discover_routes", level="WARNING") as cm:
                items = _llm_topic_items("mystoriesanimated", DiscoverRequest())
        self.assertEqual(items, [])
        self.assertIn("empty 'items'", "\n".join(cm.output))

    def test_all_items_discarded_logs_warning(self) -> None:
        # Items came back but every one is malformed (no topic field).
        # Surfaces typo-class bugs (schema field renamed, etc).
        result = {"items": [
            {"hook": "no topic field 1"},
            {"hook": "no topic field 2"},
        ]}
        with patch("control.routes.discover_routes._llm_disabled", return_value=False), \
             patch("pipeline.llm.cli.call_llm", return_value=result):
            with self.assertLogs("control.routes.discover_routes", level="WARNING") as cm:
                items = _llm_topic_items("hindutavaanimated", DiscoverRequest())
        self.assertEqual(items, [])
        self.assertIn("all were", "\n".join(cm.output))


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
