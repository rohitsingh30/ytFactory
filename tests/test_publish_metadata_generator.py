"""Pin the contract of :func:`pipeline.publish.generate_publish_metadata`.

The generator is a stub today (heuristic-only; the real editorial rules
land in ``docs/youtube_shorts_metadata_playbook.md``). These tests pin
the SHAPE — what fields must exist, the empty-input failure mode, the
non-empty-hashtags invariant — so the playbook can replace the
heuristics in a follow-up PR without breaking the modal / route /
worker contract.
"""
from __future__ import annotations

import unittest

from pipeline.publish import PublishMetadata, generate_publish_metadata
from pipeline.publish.metadata_generator import MissingMetadataInputError


class GenerateMetadataShapeTest(unittest.TestCase):
    """Every successful call must emit the full PublishMetadata shape."""

    def test_returns_publish_metadata_with_all_required_fields(self) -> None:
        script = {
            "title_options": ["I Found My Husband's Secret Diary"],
            "hook": "I never thought I'd be the kind of person who reads",
            "summary": "A wife discovers a hidden diary and the truth shatters her.",
            "topic": "husband secret diary",
        }
        meta = generate_publish_metadata(
            "job-xyz", script, channel="mystoriesanimated", variant="reddit_aita",
        )
        # Every field declared on PublishMetadata must be populated to
        # something the YouTube ``snippet``/``status`` payload accepts.
        self.assertIsInstance(meta, PublishMetadata)
        self.assertTrue(meta.title, "title must not be empty")
        self.assertLessEqual(len(meta.title), 100, "title respects YT cap")
        self.assertTrue(meta.description, "description must not be empty")
        self.assertLessEqual(len(meta.description), 5000)
        self.assertIsInstance(meta.hashtags, list)
        self.assertIsInstance(meta.tags, list)
        self.assertTrue(meta.category_id)
        self.assertTrue(meta.default_language)
        self.assertIsInstance(meta.made_for_kids, bool)

    def test_title_uses_first_title_option(self) -> None:
        script = {
            "title_options": ["The Right Title", "An Alternative"],
            "hook": "ignored hook",
            "topic": "ignored topic",
        }
        meta = generate_publish_metadata(
            "job-1", script, channel="historyrecapped", variant=None,
        )
        self.assertEqual(meta.title, "The Right Title")

    def test_title_falls_back_through_priority_chain(self) -> None:
        # No title_options, no title field → hook.
        script = {"hook": "A hook line that becomes the title"}
        meta = generate_publish_metadata(
            "job-2", script, channel="historyrecapped", variant=None,
        )
        self.assertEqual(meta.title, "A hook line that becomes the title")

        # No title sources at all → topic.
        script = {"topic": "Some Topic"}
        meta = generate_publish_metadata(
            "job-3", script, channel="historyrecapped", variant=None,
        )
        self.assertEqual(meta.title, "Some Topic")


class HashtagsNonEmptyInvariantTest(unittest.TestCase):
    """Per feedback_silent_fallback_unshippable_output: hashtags must
    never be silently empty — even if tokenisation finds no keywords,
    we anchor with at least one hashtag."""

    def test_hashtags_non_empty_even_when_title_has_no_extractable_keywords(self) -> None:
        # Title is short stop-words only; the tokeniser would drop them
        # all. The fallback ``#shorts`` anchor must still appear.
        script = {"title_options": ["the and for"], "topic": "is to be"}
        meta = generate_publish_metadata(
            "job-fallback", script, channel="auto", variant=None,
        )
        self.assertTrue(meta.hashtags, "hashtags must never be empty")
        self.assertTrue(
            all(h.startswith("#") for h in meta.hashtags),
            "every hashtag must start with #",
        )

    def test_hashtags_extracted_from_title_keywords(self) -> None:
        script = {"title_options": ["Cosmic Microwave Background Explained"]}
        meta = generate_publish_metadata(
            "job-cmb", script, channel="cosmosdecoded", variant=None,
        )
        # At least one hashtag must come from the title content (not
        # the #shorts fallback).
        non_anchor = [h for h in meta.hashtags if h != "#shorts"]
        self.assertTrue(
            non_anchor,
            f"expected at least one keyword-derived hashtag; got {meta.hashtags!r}",
        )


class TagsBudgetTest(unittest.TestCase):
    def test_tags_respect_youtube_500_char_budget(self) -> None:
        script = {
            "title_options": ["A Very Long Title With Many Keywords"],
            "hook": "supplementary keywords here for the tag pool",
            "summary": "even more distinct tokens make their way in",
        }
        meta = generate_publish_metadata(
            "job-tags", script, channel="auto", variant=None,
        )
        total = sum(len(t) + 1 for t in meta.tags)
        self.assertLessEqual(total, 500, f"tag budget overrun: {total}")
        for t in meta.tags:
            self.assertLessEqual(len(t), 30, f"tag too long: {t!r}")


class MissingInputRaisesTest(unittest.TestCase):
    """Per feedback_silent_fallback_unshippable_output: no title source
    AND no topic must RAISE, not ship an empty-titled publish payload."""

    def test_raises_when_no_title_and_no_topic(self) -> None:
        with self.assertRaises(MissingMetadataInputError):
            generate_publish_metadata(
                "job-empty",
                {"summary": "summary alone is not a title source"},
                channel="auto",
                variant=None,
            )

    def test_raises_when_script_is_empty_dict(self) -> None:
        with self.assertRaises(MissingMetadataInputError):
            generate_publish_metadata("job-empty2", {}, channel="auto", variant=None)

    def test_raises_when_title_options_present_but_blank(self) -> None:
        # All title_options entries are whitespace, no other title sources.
        with self.assertRaises(MissingMetadataInputError):
            generate_publish_metadata(
                "job-blank",
                {"title_options": ["", "   "], "topic": ""},
                channel="auto",
                variant=None,
            )

    def test_type_error_on_non_dict_script(self) -> None:
        with self.assertRaises(TypeError):
            generate_publish_metadata(
                "job-bad-script", "not a dict", channel="auto", variant=None,  # type: ignore[arg-type]
            )


class DescriptionContainsHashtagsTest(unittest.TestCase):
    """YouTube Shorts only shows above-title hashtags when they appear
    in the description body. Pin that the generator embeds them."""

    def test_description_contains_at_least_one_hashtag(self) -> None:
        script = {
            "title_options": ["Apollo Eleven Touchdown"],
            "summary": "The Eagle landed at Tranquility Base on July 20 1969.",
        }
        meta = generate_publish_metadata(
            "job-apollo", script, channel="historyrecapped", variant=None,
        )
        self.assertTrue(meta.hashtags)
        self.assertIn(
            meta.hashtags[0], meta.description,
            "description must embed the generated hashtags so YouTube "
            "Shorts shows them above the title",
        )


if __name__ == "__main__":
    unittest.main()
