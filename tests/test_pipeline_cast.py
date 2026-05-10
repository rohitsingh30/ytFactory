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


# ---- Additional coverage for authoring and dossier transforms ----

import shutil
from unittest.mock import patch

from pipeline.llm import cast as cast_mod


SCRATCH_CAST = PROJECT_ROOT / "tests" / ".scratch_cast"


class AuthorCastTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH_CAST, ignore_errors=True)
        SCRATCH_CAST.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH_CAST, ignore_errors=True)

    def test_author_cast_success_writes_and_warns_on_conflicts(self):
        raw = {
            "narrator": {"description": "long short loose ponytail hair", "default_emotion": "tense", "age_band": "adult", "gender": "female"},
            "supporting": [{"name": "Bob", "description": "brown hair down in a bun"}, "skip"],
        }
        out = SCRATCH_CAST / "cast" / "cast.json"
        with patch.object(cast_mod.llm, "model_for", return_value="opus") as model_for, \
             patch.object(cast_mod.llm, "call_claude_cli", return_value=raw) as call:
            result = cast_mod.author_cast(raw_story={"slug": "s", "title": "T", "body": "B"}, channel_cfg={"image_style_prefix": "style"}, out_path=out)
        self.assertEqual(result["narrator"]["age_band"], "adult")
        self.assertTrue(out.exists())
        model_for.assert_called_once_with("cast")
        self.assertEqual(call.call_args.kwargs["model"], "opus")

    def test_author_cast_validation_errors(self):
        with self.assertRaises(ValueError):
            cast_mod.author_cast(raw_story={"slug": "x"}, channel_cfg={"image_style_prefix": "style"}, out_path=SCRATCH_CAST / "x.json")
        with self.assertRaises(ValueError):
            cast_mod.author_cast(raw_story={"body": "b"}, channel_cfg={}, out_path=SCRATCH_CAST / "x.json")
        bad_outputs = [[], {"supporting": []}, {"narrator": {}}, {"narrator": {"description": ""}}]
        for raw in bad_outputs:
            with patch.object(cast_mod.llm, "model_for", return_value="opus"), patch.object(cast_mod.llm, "call_claude_cli", return_value=raw):
                with self.assertRaises(ValueError):
                    cast_mod.author_cast(raw_story={"body": "b"}, channel_cfg={"image_style_prefix": "style"}, out_path=SCRATCH_CAST / "x.json")


class SelfConsistencyAndDossierTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH_CAST, ignore_errors=True)
        SCRATCH_CAST.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH_CAST, ignore_errors=True)

    def test_lint_self_consistency_empty_clean_and_conflicts(self):
        self.assertEqual(cast_mod._lint_self_consistency(""), [])
        self.assertEqual(cast_mod._lint_self_consistency("short brown hair"), [])
        warnings = cast_mod._lint_self_consistency("long short hair worn loose and tied in a ponytail")
        self.assertTrue(any("multiple hair lengths" in w for w in warnings))
        self.assertTrue(any("conflicting hairstyles" in w for w in warnings))

    def test_person_to_supporting_flattens_visual_dedupes_aliases_and_seed(self):
        person = {
            "name": "Alex",
            "aliases": ["Alex", "A."],
            "seed": 123,
            "visual": {
                "body": "tall striker",
                "hair": "curly hair",
                "facial_hair": "trimmed beard",
                "kit": "blue kit",
                "shirt_number": 9,
                "era_notes": "night final",
            },
        }
        out = cast_mod._person_to_supporting(person)
        self.assertEqual(out["aliases"], ["Alex", "A."])
        self.assertEqual(out["seed"], 123)
        self.assertIn("shirt #9", out["description"])
        self.assertLessEqual(len(out["description"]), 280)
        no_dup = cast_mod._person_to_supporting({"name": "Beard", "visual": {"body": "trimmed beard", "facial_hair": "trimmed beard"}})
        self.assertEqual(no_dup["description"], "trimmed beard")

    def test_cast_from_dossier_success_default_and_errors(self):
        with self.assertRaises(ValueError):
            cast_mod.cast_from_dossier(dossier={}, channel_cfg={}, out_path=SCRATCH_CAST / "bad.json")
        dossier = {"people": [
            {"name": "Alex", "visual": {"kit": "red kit"}},
            {"visual": {"kit": "skip no name"}},
            "skip non dict",
        ]}
        out = SCRATCH_CAST / "dossier" / "cast.json"
        cast = cast_mod.cast_from_dossier(dossier=dossier, channel_cfg={"narrator_persona": "Custom narrator"}, out_path=out)
        self.assertEqual(cast["narrator"]["description"], "Custom narrator")
        self.assertEqual(len(cast["supporting"]), 1)
        self.assertTrue(out.exists())
        default = cast_mod.cast_from_dossier(dossier={"people": []}, channel_cfg={}, out_path=SCRATCH_CAST / "default.json")
        self.assertIn("Football analyst", default["narrator"]["description"])


if __name__ == "__main__":
    unittest.main()
