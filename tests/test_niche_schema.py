"""Universal NicheVideo schema — same shape, different values per niche.

We prove the schema is genuinely universal by round-tripping a
realistic payload for 5 distinct niches with very different content
(history Short, AITA Short, Reddit thread, kathaa long-form, sports
last5 countdown). If the schema fits all five without contortion,
adding a 6th is just data.
"""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from pipeline.schemas.niche_schema import (
    NICHE_REGISTRY,
    NicheVideo,
    list_niches,
    niche_defaults,
    schema_dict,
    validate_payload,
)


def _history_short_payload() -> dict:
    return {
        "schema_version": "1",
        "channel": "historyrecapped",
        "niche": "history-short",
        "slug": "apollo-11-1969",
        "title": {
            "options": [
                "How Apollo 11 Almost Missed the Moon",
                "20 Seconds From Aborting the Moon Landing",
                "Apollo 11: The Last 60 Seconds",
            ],
        },
        "hook": "With sixty seconds of fuel left, Armstrong sees the landing site is full of boulders.",
        "narration": {
            "text": (
                "Sea of Tranquility. The Moon. 20 July 1969. "
                "The whole world is watching. A quarter of humanity, live. "
                "Two men in a four-legged lander. Forty thousand engineers behind them. "
                "Twelve minutes from descent. Computer alarms keep firing. Mission Control overrides them. "
                "With sixty seconds of fuel left, Armstrong sees the chosen landing site is full of boulders. "
                "He flies past it. "
                "Twenty seconds of fuel left. He picks a flat patch and sets her down. "
                "The Eagle has landed. "
                "Six hundred million people watch a man step off a ladder. "
                "The footprints are still there, fifty years later. "
                "LIKE if you learned something. SUBSCRIBE for more such stories."
            ),
            "word_count": 132,
            "duration_target_s": 56.0,
        },
        "beats": [
            {"index": 0, "role": "place_date", "text": "Sea of Tranquility. The Moon. 20 July 1969."},
            {"index": 1, "role": "stakes", "text": "The whole world is watching. A quarter of humanity, live."},
            {"index": 2, "role": "subject", "text": "Two men in a four-legged lander. Forty thousand engineers behind them."},
            {"index": 3, "role": "plan", "text": "Twelve minutes from descent. Computer alarms keep firing. Mission Control overrides them."},
            {"index": 4, "role": "complication", "text": "With sixty seconds of fuel left, Armstrong sees the chosen landing site is full of boulders."},
            {"index": 5, "role": "action", "text": "He flies past it."},
            {"index": 6, "role": "twist", "text": "Twenty seconds of fuel left. He picks a flat patch and sets her down."},
            {"index": 7, "role": "resolution", "text": "The Eagle has landed."},
            {"index": 8, "role": "cost", "text": "Six hundred million people watch a man step off a ladder."},
            {"index": 9, "role": "meaning", "text": "The footprints are still there, fifty years later."},
            {"index": 10, "role": "closer", "text": "LIKE if you learned something. SUBSCRIBE for more such stories."},
        ],
        "closer": {
            "spoken": "LIKE if you learned something. SUBSCRIBE for more such stories.",
            "style": "general",
        },
        "render": {
            "pipeline": "footage_only",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
        },
        "metadata": {
            "sources": [
                {"url": "https://en.wikipedia.org/wiki/Apollo_11", "type": "wikipedia"},
            ],
            "pronunciation_notes": "",
        },
        "validation": niche_defaults("history-short")["default_validation"],
    }


def _aita_animated_payload() -> dict:
    return {
        "schema_version": "1",
        "channel": "mystoriesanimated",
        "niche": "aita-animated",
        "slug": "aita-birthday-cake",
        "title": {"options": ["Am I wrong for ruining the cake?"]},
        "hook": "My SIL asked me to bake the wedding cake. Then the bridezilla started.",
        "narration": {
            "text": (
                "My SIL asked me to bake her wedding cake. I said yes. "
                "Then she started sending me Pinterest boards at 2am. "
                "Tier counts changed three times. Color swatches changed five times. "
                "Day before the wedding, she demanded a fourth tier. "
                "I baked three. I delivered three. "
                "She melted down at the reception. "
                "LIKE if you'd do the same. COMMENT what you'd do."
            ),
            "word_count": 64,
        },
        "beats": [
            {"index": 0, "role": "hook", "text": "My SIL asked me to bake her wedding cake. I said yes."},
            {"index": 1, "role": "context", "text": "Then she started sending me Pinterest boards at 2am."},
            {"index": 2, "role": "incident", "text": "Tier counts changed three times. Color swatches changed five times."},
            {"index": 3, "role": "conflict", "text": "Day before the wedding, she demanded a fourth tier."},
            {"index": 4, "role": "decision", "text": "I baked three. I delivered three."},
            {"index": 5, "role": "twist", "text": "She melted down at the reception."},
            {"index": 6, "role": "closer", "text": "LIKE if you'd do the same. COMMENT what you'd do."},
        ],
        "closer": {
            "spoken": "LIKE if you'd do the same. COMMENT what you'd do.",
            "style": "aita-vote",
        },
        "render": {
            "pipeline": "shorts",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
            "image_provider": "cloudrun_z_image_turbo",
        },
        "validation": niche_defaults("aita-animated")["default_validation"],
    }


def _reddit_thread_payload() -> dict:
    """ScrollPulse: a Reddit post + comments rendered as split-screen
    cards. Beats are the post + each comment in turn."""
    return {
        "schema_version": "1",
        "channel": "scrollpulse",
        "niche": "reddit-thread",
        "slug": "askreddit-guam-2026",
        "title": {"options": ["Reddit Roasts Guam"]},
        "hook": "AskReddit asked the dumbest country and got an unexpected winner.",
        "narration": {
            "text": (
                "AskReddit asked: what's the most genuinely unintelligent country? "
                "Top comment: Guam, but only because they're not technically a country. "
                "Second: at least Guam knows what time zone it's in. "
                "Third: Guam national pastime is asking if Guam is a country. "
                "Fourth: leave Guam alone. "
                "LIKE if you laughed. SUBSCRIBE for more brain rot."
            ),
            "word_count": 50,
        },
        "beats": [
            {
                "index": 0, "role": "post",
                "text": "AskReddit asked: what's the most genuinely unintelligent country?",
                "extras": {"author": "u/GuamHater42", "subreddit": "AskReddit"},
            },
            {
                "index": 1, "role": "comment",
                "text": "Guam, but only because they're not technically a country.",
                "extras": {"author": "u/PacificFacts", "upvotes": 18420},
            },
            {
                "index": 2, "role": "comment",
                "text": "At least Guam knows what time zone it's in.",
                "extras": {"author": "u/Chronos", "upvotes": 9211},
            },
            {
                "index": 3, "role": "comment",
                "text": "Guam national pastime is asking if Guam is a country.",
                "extras": {"author": "u/MetaJoke", "upvotes": 4502},
            },
            {
                "index": 4, "role": "comment",
                "text": "Leave Guam alone.",
                "extras": {"author": "u/GuamDefender", "upvotes": 3014},
            },
            {
                "index": 5, "role": "closer",
                "text": "LIKE if you laughed. SUBSCRIBE for more brain rot.",
            },
        ],
        "closer": {
            "spoken": "LIKE if you laughed. SUBSCRIBE for more brain rot.",
            "style": "brain-rot",
        },
        "render": {
            "pipeline": "split_screen",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
        },
    }


def _kathaa_payload() -> dict:
    """A 50-70 min Hindu kathaa rendered as long-form chapters."""
    return {
        "schema_version": "1",
        "channel": "hindutavaanimated",
        "niche": "hindutava-katha",
        "slug": "ramayan-sundarkand-202605",
        "title": {"options": ["Sundarkand — Hanuman's Leap"]},
        "hook": "Hanuman crosses the ocean.",
        "narration": {
            "text": "Adhyay 1. ... Adhyay 10. Hari Om Tat Sat.",
            "word_count": 8000,
            "duration_target_s": 3600.0,
        },
        "beats": [
            {"index": i, "role": f"chapter_{i + 1}",
             "text": f"Adhyay {i + 1}. <chapter prose>", "key_visual": ""}
            for i in range(10)
        ] + [{"index": 10, "role": "closer", "text": "Hari Om Tat Sat."}],
        "closer": {"spoken": "Hari Om Tat Sat.", "style": "mythological"},
        "render": {
            "pipeline": "long_form",
            "aspect": "16:9",
            "tts_provider": "cloudrun_indicf5",
            "tts_voice": "hindutava",
        },
    }


def _sports_last5_payload() -> dict:
    return {
        "schema_version": "1",
        "channel": "sportsrecapped",
        "niche": "sports-last5",
        "slug": "ucl-finals-2020-2024",
        "title": {"options": ["Last 5 Champions League Finals"]},
        "hook": "From Bayern's empty Lisbon to Madrid's 15th.",
        "narration": {"text": "Number 5: 2020, Bayern beat PSG. ... Real Madrid wins 15.", "word_count": 60},
        "beats": [
            {"index": 0, "role": "hook", "text": "From Bayern's empty Lisbon to Madrid's 15th."},
            {"index": 1, "role": "rank_5", "text": "Number 5: 2020, Bayern beat PSG."},
            {"index": 2, "role": "rank_4", "text": "Number 4: 2021, Chelsea beat City."},
            {"index": 3, "role": "rank_3", "text": "Number 3: 2022, Real Madrid beat Liverpool."},
            {"index": 4, "role": "rank_2", "text": "Number 2: 2023, City beat Inter."},
            {"index": 5, "role": "rank_1", "text": "Number 1: 2024, Real Madrid beat Dortmund."},
            {"index": 6, "role": "closer", "text": "LIKE for the last 5. SUBSCRIBE for the next 5."},
        ],
        "closer": {"spoken": "LIKE for the last 5. SUBSCRIBE for the next 5.", "style": "general"},
        "render": {"pipeline": "footage_only", "aspect": "9:16"},
        "validation": niche_defaults("sports-last5")["default_validation"],
    }


# ---------------------------------------------------------------------------


class NicheVideoUniversalityTest(unittest.TestCase):
    """Same schema, 5 wildly different niches — all should validate."""

    def test_history_short_round_trips(self) -> None:
        v = validate_payload(_history_short_payload())
        self.assertEqual(v.niche, "history-short")
        self.assertEqual(len(v.beats), 11)
        self.assertEqual(v.beats[0].role, "place_date")

    def test_aita_animated_round_trips(self) -> None:
        v = validate_payload(_aita_animated_payload())
        self.assertEqual(v.niche, "aita-animated")
        self.assertEqual(v.closer.style, "aita-vote")

    def test_reddit_thread_round_trips(self) -> None:
        v = validate_payload(_reddit_thread_payload())
        self.assertEqual(v.beats[1].extras["author"], "u/PacificFacts")
        self.assertEqual(v.render.pipeline, "split_screen")

    def test_kathaa_round_trips(self) -> None:
        v = validate_payload(_kathaa_payload())
        self.assertEqual(v.beats[0].role, "chapter_1")
        self.assertEqual(v.beats[-1].role, "closer")

    def test_sports_last5_round_trips(self) -> None:
        v = validate_payload(_sports_last5_payload())
        roles = [b.role for b in v.beats]
        self.assertIn("rank_5", roles)
        self.assertIn("rank_1", roles)


class NicheVideoValidationTest(unittest.TestCase):
    """Schema rejects malformed payloads."""

    def test_missing_required_field_rejected(self) -> None:
        bad = _history_short_payload()
        del bad["narration"]
        with self.assertRaises(ValidationError):
            validate_payload(bad)

    def test_beats_must_be_indexed_in_order(self) -> None:
        bad = _history_short_payload()
        bad["beats"][0]["index"] = 5
        with self.assertRaises(ValidationError) as ctx:
            validate_payload(bad)
        self.assertIn("indexed", str(ctx.exception))

    def test_word_count_band_enforced_when_set(self) -> None:
        bad = _history_short_payload()
        bad["narration"]["word_count"] = 999  # niche band is [130, 138]
        with self.assertRaises(ValidationError) as ctx:
            validate_payload(bad)
        self.assertIn("word_count", str(ctx.exception))

    def test_closer_literal_must_match_options(self) -> None:
        bad = _history_short_payload()
        bad["closer"]["spoken"] = "Hit that subscribe button bro!"
        with self.assertRaises(ValidationError) as ctx:
            validate_payload(bad)
        self.assertIn("closer", str(ctx.exception).lower())

    def test_banned_phrase_rejected(self) -> None:
        bad = _history_short_payload()
        bad["narration"]["text"] = bad["narration"]["text"] + " smash that subscribe button"
        with self.assertRaises(ValidationError) as ctx:
            validate_payload(bad)
        self.assertIn("banned", str(ctx.exception).lower())

    def test_required_beat_role_missing(self) -> None:
        bad = _history_short_payload()
        # Drop the "twist" beat (role is required by registry default).
        bad["beats"] = [b for b in bad["beats"] if b["role"] != "twist"]
        # Re-index to keep beats[0..N-1] consecutive.
        for i, b in enumerate(bad["beats"]):
            b["index"] = i
        with self.assertRaises(ValidationError) as ctx:
            validate_payload(bad)
        self.assertIn("twist", str(ctx.exception))

    def test_slug_pattern_enforced(self) -> None:
        bad = _history_short_payload()
        bad["slug"] = "../../etc/passwd"
        with self.assertRaises(ValidationError):
            validate_payload(bad)

    def test_footage_window_out_must_exceed_in(self) -> None:
        bad = _history_short_payload()
        bad["beats"][0]["footage_window"] = {"source_url": "x", "in_s": 10, "out_s": 5}
        with self.assertRaises(ValidationError):
            validate_payload(bad)


class NicheRegistryTest(unittest.TestCase):
    def test_at_least_15_niches_registered(self) -> None:
        # We expect ~19 — keep a floor so a regression that wipes the
        # registry mid-edit gets caught.
        self.assertGreaterEqual(len(list_niches()), 15)

    def test_every_registered_niche_maps_to_known_channel(self) -> None:
        # Single canonical slug per channel — no alias resolution.
        # If this test fails, fix the offending niche to use the
        # canonical slug from pipeline/channels.yaml.
        from pipeline.channels import all_channels
        known = {c.slug for c in all_channels()}
        for niche, defaults in NICHE_REGISTRY.items():
            self.assertIn(
                defaults["channel"], known,
                f"niche {niche!r} channel {defaults['channel']!r} not in "
                f"pipeline/channels.yaml (known: {sorted(known)})",
            )

    def test_every_registered_niche_has_default_render(self) -> None:
        for niche, defaults in NICHE_REGISTRY.items():
            self.assertIn("default_render", defaults, f"{niche} missing default_render")
            self.assertIn("pipeline", defaults["default_render"], f"{niche} default_render missing pipeline")

    def test_schema_dict_export_works(self) -> None:
        # JSON Schema export — useful for web form generators / docs.
        s = schema_dict()
        self.assertEqual(s["title"], "NicheVideo")
        self.assertIn("properties", s)
        self.assertIn("beats", s["properties"])


class NicheRegistryHelperTest(unittest.TestCase):
    def test_niche_defaults_known(self) -> None:
        d = niche_defaults("history-short")
        self.assertIn("default_render", d)
        self.assertEqual(d["default_render"]["pipeline"], "footage_only")

    def test_niche_defaults_unknown_returns_empty(self) -> None:
        # Forward-compat: a not-yet-registered niche shouldn't crash
        # the producer; it just gets no defaults.
        self.assertEqual(niche_defaults("not-yet-a-niche"), {})


class TitleValidatorTest(unittest.TestCase):
    """Line 53: Title._selected_in_range validator raises ValueError."""

    def test_selected_index_out_of_range_raises(self):
        from pydantic import ValidationError
        from pipeline.schemas.niche_schema import Title
        with self.assertRaises(ValidationError):
            Title(options=["opt1", "opt2"], selected_index=5)

    def test_selected_index_in_range_passes(self):
        from pipeline.schemas.niche_schema import Title
        t = Title(options=["opt1", "opt2"], selected_index=1)
        self.assertEqual(t.selected_index, 1)


class FootageWindowValidatorTest(unittest.TestCase):
    """Line 81: FootageWindow._out_after_in happy path returns self."""

    def test_out_after_in_passes(self):
        from pipeline.schemas.niche_schema import FootageWindow
        fw = FootageWindow(source_url="https://x.com/video", in_s=0.0, out_s=5.0)
        self.assertEqual(fw.out_s, 5.0)

    def test_out_before_in_raises(self):
        from pydantic import ValidationError
        from pipeline.schemas.niche_schema import FootageWindow
        with self.assertRaises(ValidationError):
            FootageWindow(source_url="https://x.com/video", in_s=5.0, out_s=3.0)


if __name__ == "__main__":
    unittest.main()

