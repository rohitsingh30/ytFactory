from __future__ import annotations

import json
import shutil
import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm.segment import Story, load_stories, save_stories, transcript_text_for_segmenting


SCRATCH = PROJECT_ROOT / "tests" / ".scratch_segment"


class SegmentStoriesTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_save_and_load_roundtrip_creates_parent(self):
        path = SCRATCH / "nested" / "stories.json"
        stories = [Story(title="One", start=1.5, end=9.0, summary="A story")]
        save_stories(stories, path)
        self.assertTrue(path.exists())
        self.assertEqual(load_stories(path), stories)
        raw = json.loads(path.read_text())
        self.assertEqual(raw[0]["title"], "One")

    def test_transcript_text_formats_timestamps_and_strips_text(self):
        result = {
            "segments": [
                {"start": 0, "text": " hello "},
                {"start": 65.9, "text": "world"},
                {"start": 3661.2, "text": " later "},
                {"text": "missing start"},
            ]
        }
        self.assertEqual(
            transcript_text_for_segmenting(result),
            "[00:00:00] hello\n[00:01:05] world\n[01:01:01] later\n[00:00:00] missing start",
        )

    def test_empty_segments_return_empty_string(self):
        self.assertEqual(transcript_text_for_segmenting({}), "")


if __name__ == "__main__":
    unittest.main()
