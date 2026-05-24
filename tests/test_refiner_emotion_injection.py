"""Regression tests for the emotion-cue injection in
``pipeline.images.prompt_refiner`` (v5-subject-emotion, 2026-05-24).

Surfaces preflight 88d98126 grievance #2 — the protagonist's face was
the same concerned/sad expression in every panel even as the narration
escalated proud → outraged → defeated. The author wasn't emitting an
emotion token and the refiner had nothing deterministic to inject.

These tests pin that:

  * Every allowed emotion token resolves to a non-empty posture/face
    cue via ``emotion_cue``.
  * The cue is DIFFERENT across the load-bearing emotion tokens
    (proud vs outraged vs defeated) — otherwise the diffusion model
    renders the same face regardless.
  * The cue is appended to ``refined_visual`` at refine time so a
    cached prompts.json carries the expression lock.
  * The cue is OMITTED for ``subject=scene`` beats (no human focal
    subject; appending a posture clause to an environment-only beat
    is noise on the diffusion model).
  * Cache invalidation (compute_input_hash) sees the emotion token —
    a critic-patched emotion auto-invalidates the cached refined_*.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path

from pipeline.images import images as _images_mod
from pipeline.images import prompt_refiner as refiner
from pipeline.llm import prompts as pr


class EmotionCueLookupTest(unittest.TestCase):
    """The lookup table itself is the contract — every allowed emotion
    must have a deterministic cue, and the cues must be DISTINCT enough
    that the diffusion model actually renders different expressions."""

    def test_every_allowed_emotion_has_a_cue(self):
        for token in pr.ALLOWED_EMOTIONS:
            cue = refiner.emotion_cue(token)
            self.assertTrue(
                cue, f"emotion {token!r} resolves to empty cue; the "
                "ALLOWED_EMOTIONS list and _EMOTION_CUES table have "
                "drifted out of sync"
            )
            # Each cue must mention at least one face/posture vocabulary
            # term — without this the LLM-author Rule 19 was implicitly
            # the only place emotion → expression mapping was made, and
            # that's exactly what failed on 88d98126.
            self.assertTrue(
                any(token in cue.lower() for token in (
                    "lips", "mouth", "eyes", "brow", "shoulders", "jaw",
                    "chin", "head", "chest", "eyelid",
                )),
                f"cue for {token!r} lacks face/posture vocabulary: {cue!r}",
            )

    def test_unknown_emotion_falls_back_to_neutral_cue(self):
        # Per the spec: "if the LLM fails to emit subject/emotion for a
        # beat, fall back to "protagonist" + "neutral" per-beat" — so
        # unknown emotions must not crash, they must resolve to a
        # known cue.
        self.assertEqual(
            refiner.emotion_cue("livid"),  # synonym, not in vocabulary
            refiner.emotion_cue("neutral"),
        )
        self.assertEqual(refiner.emotion_cue(None), refiner.emotion_cue("neutral"))
        self.assertEqual(refiner.emotion_cue(""), refiner.emotion_cue("neutral"))

    def test_proud_and_outraged_and_defeated_cues_are_distinct(self):
        """The load-bearing 88d98126 trio — without per-beat distinct
        cues, the protagonist's face stays the same as the story
        escalates. Pin that these three render DIFFERENT physical
        features."""
        proud = refiner.emotion_cue("proud")
        outraged = refiner.emotion_cue("outraged")
        defeated = refiner.emotion_cue("defeated")
        self.assertNotEqual(proud, outraged)
        self.assertNotEqual(outraged, defeated)
        self.assertNotEqual(proud, defeated)

    def test_outraged_cue_describes_observable_outrage_features(self):
        """The 88d98126 critique specifically called out the ketchup
        beat (which should be 'outraged'). The cue must include the
        physical hallmarks of outrage: tightened mouth / wide eyes /
        knit brow / recoil, so the diffusion model has something to
        render."""
        cue = refiner.emotion_cue("outraged").lower()
        # At least one mouth + one eye + one brow + one body cue.
        self.assertTrue(
            "mouth" in cue or "lips" in cue,
            f"outraged cue must specify mouth/lips: {cue!r}",
        )
        self.assertIn("eyes", cue, f"outraged cue must specify eyes: {cue!r}")
        # Should NOT collapse to a neutral or sad register.
        self.assertNotIn("relaxed", cue)
        self.assertNotIn("calm", cue)

    def test_proud_cue_describes_pride_features(self):
        cue = refiner.emotion_cue("proud").lower()
        self.assertTrue(
            "smile" in cue or "lift" in cue or "lifted" in cue or "tilt" in cue,
            f"proud cue must specify upward/open features: {cue!r}",
        )
        # Should NOT collapse to a defeated register.
        self.assertNotIn("dropped", cue)
        self.assertNotIn("slumped", cue)


class EmotionCueInjectedIntoRefinedVisualTest(unittest.TestCase):
    """The cue isn't useful unless it actually lands in
    ``refined_visual`` so the diffusion model sees it. Pin the wiring."""

    def setUp(self):
        # Pass strip_text_bait through unchanged for these tests; the
        # bait-stripping path is exercised in test_prompt_refiner.py.
        patcher = patch.object(_images_mod, "strip_text_bait",
                               lambda s: (s, []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _llm_returning(self, refined_visual, refined_scene="no readable text in image. setting"):
        def _llm(_prompt, *, output_json=False, **_kw):
            return {"refined_beats": [{
                "refined_visual": refined_visual,
                "refined_scene": refined_scene,
                "style_block": "Style: x. Mood: y.",
            }]}
        return _llm

    def test_outraged_emotion_appends_disbelief_cue(self):
        out = refiner.refine_prompts_batch(
            [{
                "key_visual": "kv", "scene": "kitchen",
                "subject": "protagonist",
                "emotion": "outraged",
                "narration_line": "he poured ketchup over my stew",
            }],
            era_anchor_prefix=None,
            character_description=None,
            style="x", mood="y",
            llm_call=self._llm_returning("medium shot of the character at the counter"),
        )
        rv = out[0]["refined_visual"]
        # The deterministic outraged cue must surface in the wire
        # prompt. We pin on a specific hallmark of the table entry
        # for 'outraged' — "eyes wide with disbelief".
        self.assertIn("eyes wide with disbelief", rv,
                      f"outraged cue not injected into refined_visual: {rv!r}")
        # The diagnostic emotion_cue field on the output should also
        # carry the same text.
        self.assertEqual(out[0]["emotion_cue"], refiner.emotion_cue("outraged"))

    def test_proud_emotion_appends_triumphant_smile_cue(self):
        out = refiner.refine_prompts_batch(
            [{
                "key_visual": "kv", "scene": "kitchen",
                "subject": "protagonist",
                "emotion": "proud",
                "narration_line": "I cooked the stew for hours",
            }],
            era_anchor_prefix=None,
            character_description=None,
            style="x", mood="y",
            llm_call=self._llm_returning("medium shot of the character presenting a bowl"),
        )
        rv = out[0]["refined_visual"]
        # Pin on a specific hallmark of 'proud': "triumphant smile".
        self.assertIn("triumphant smile", rv,
                      f"proud cue not injected into refined_visual: {rv!r}")

    def test_defeated_emotion_appends_slumped_cue(self):
        out = refiner.refine_prompts_batch(
            [{
                "key_visual": "kv", "scene": "kitchen",
                "subject": "protagonist",
                "emotion": "defeated",
                "narration_line": "I sat alone with the bowl",
            }],
            era_anchor_prefix=None,
            character_description=None,
            style="x", mood="y",
            llm_call=self._llm_returning("medium shot of the character at the table"),
        )
        rv = out[0]["refined_visual"]
        # Pin on the defeated hallmark: dropped head / slumped /
        # rounded shoulders.
        self.assertTrue(
            "shoulders rounded" in rv or "dropped" in rv,
            f"defeated cue not injected: {rv!r}",
        )

    def test_emotion_cue_skipped_for_subject_scene(self):
        """Environment-only beats (subject=scene) must not gain a
        posture/face clause — there's no human focal subject in frame."""
        out = refiner.refine_prompts_batch(
            [{
                "key_visual": "kv", "scene": "kitchen",
                "subject": "scene",
                "emotion": "tense",
                "narration_line": "the kitchen sat in silence",
            }],
            era_anchor_prefix=None,
            character_description=None,
            style="x", mood="y",
            llm_call=self._llm_returning("wide shot of an empty kitchen"),
        )
        rv = out[0]["refined_visual"]
        # The cue must NOT be appended.
        self.assertNotIn("jaw clenched", rv,
                         f"emotion cue should be skipped for subject=scene: {rv!r}")
        self.assertNotIn("shoulders raised", rv,
                         f"emotion cue should be skipped for subject=scene: {rv!r}")

    def test_proud_and_outraged_produce_different_refined_visuals(self):
        """Direct grievance #2 pin: changing ONLY the emotion token
        between two otherwise-identical beats must produce DIFFERENT
        refined_visual strings — otherwise the rendered face is the
        same across the proud → outraged escalation."""
        common_beat = {
            "key_visual": "kv", "scene": "kitchen",
            "subject": "protagonist",
            "narration_line": "she stood at the counter",
        }
        out_proud = refiner.refine_prompts_batch(
            [{**common_beat, "emotion": "proud"}],
            era_anchor_prefix=None,
            character_description=None,
            style="x", mood="y",
            llm_call=self._llm_returning("medium shot of the character"),
        )
        out_outraged = refiner.refine_prompts_batch(
            [{**common_beat, "emotion": "outraged"}],
            era_anchor_prefix=None,
            character_description=None,
            style="x", mood="y",
            llm_call=self._llm_returning("medium shot of the character"),
        )
        self.assertNotEqual(
            out_proud[0]["refined_visual"],
            out_outraged[0]["refined_visual"],
            "proud and outraged emotion tokens must produce different "
            "refined_visual strings — otherwise the rendered expression "
            "is the same across the story arc (88d98126 grievance #2)",
        )


class EmotionAndSubjectInvalidateCacheHashTest(unittest.TestCase):
    """Per the spec: REFINER_VERSION bumped to v5-subject-emotion so
    caches pre-dating subject+emotion auto-invalidate. Additionally, a
    critic-patched emotion or subject on a cached beat must invalidate
    the hash so the stale refined_visual doesn't survive the patch."""

    def test_version_is_v5_subject_emotion(self):
        self.assertEqual(refiner.REFINER_VERSION, "v5-subject-emotion")

    def test_hash_changes_when_emotion_changes(self):
        common = dict(
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        h_proud = refiner.compute_input_hash(
            beat={"key_visual": "kv", "scene": "s", "subject": "protagonist",
                  "emotion": "proud"},
            **common,
        )
        h_outraged = refiner.compute_input_hash(
            beat={"key_visual": "kv", "scene": "s", "subject": "protagonist",
                  "emotion": "outraged"},
            **common,
        )
        self.assertNotEqual(
            h_proud, h_outraged,
            "hash MUST encode emotion; without this, a critic-patched "
            "emotion would still honour the stale refined_visual from "
            "the previous emotion."
        )

    def test_hash_changes_when_subject_changes(self):
        common = dict(
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        h_proto = refiner.compute_input_hash(
            beat={"key_visual": "kv", "scene": "s", "subject": "protagonist",
                  "emotion": "neutral"},
            **common,
        )
        h_partner = refiner.compute_input_hash(
            beat={"key_visual": "kv", "scene": "s", "subject": "partner",
                  "emotion": "neutral"},
            **common,
        )
        self.assertNotEqual(
            h_proto, h_partner,
            "hash MUST encode subject; without this, switching subject "
            "from protagonist→partner would still honour the stale "
            "protagonist-centric refined_visual.",
        )


class SubjectAndEmotionCueSurfaceInOutputTest(unittest.TestCase):
    """The output dict must expose ``subject_lead`` and ``emotion_cue``
    diagnostic fields so render-time inspectors and critic tooling can
    audit what got woven in without diffing the wire prompt."""

    def setUp(self):
        patcher = patch.object(_images_mod, "strip_text_bait",
                               lambda s: (s, []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_output_includes_subject_lead_and_emotion_cue_diagnostics(self):
        def fake_llm(_prompt, *, output_json=False, **_kw):
            return {"refined_beats": [{
                "refined_visual": "medium shot",
                "refined_scene": "no readable text in image. setting",
                "style_block": "Style: x. Mood: y.",
            }]}
        out = refiner.refine_prompts_batch(
            [{"key_visual": "kv", "scene": "s",
              "subject": "partner", "emotion": "outraged",
              "narration_line": "he poured ketchup (32M)"}],
            era_anchor_prefix=None,
            character_description=None,
            style="x", mood="y",
            llm_call=fake_llm,
        )
        self.assertIn("subject_lead", out[0])
        self.assertIn("emotion_cue", out[0])
        # Diagnostic emotion_cue must equal the lookup-table entry.
        self.assertEqual(out[0]["emotion_cue"], refiner.emotion_cue("outraged"))


if __name__ == "__main__":
    unittest.main()
