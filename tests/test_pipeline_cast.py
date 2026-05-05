"""Tests for pipeline.cast — only the loader. The author path calls
the `claude` CLI subprocess; that's integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm.cast import load_cast


class LoadCastTest(unittest.TestCase):
    def test_missing_file_returns_none(self):
        self.assertIsNone(load_cast(Path("/tmp/_no_such_cast.json")))

    def test_well_formed_cast_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cast.json"
            p.write_text(json.dumps({
                "narrator": {
                    "description": "round-headed cartoon mum, brown hair, yellow shirt",
                    "default_emotion": "tense",
                    "age_band": "middle-aged",
                    "gender": "female",
                },
                "supporting": [],
            }))
            cast = load_cast(p)
            self.assertIsNotNone(cast)
            self.assertEqual(cast["narrator"]["age_band"], "middle-aged")

    def test_missing_narrator_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cast.json"
            p.write_text(json.dumps({"supporting": []}))
            self.assertIsNone(load_cast(p))

    def test_empty_description_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cast.json"
            p.write_text(json.dumps({"narrator": {"description": ""}}))
            self.assertIsNone(load_cast(p))

    def test_malformed_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cast.json"
            p.write_text("{not json}")
            self.assertIsNone(load_cast(p))


if __name__ == "__main__":
    unittest.main()
