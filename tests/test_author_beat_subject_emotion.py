"""Regression tests for the per-beat ``subject`` + ``emotion`` schema
(v5-subject-emotion, 2026-05-24).

Surfaces:

- Preflight 88d98126 grievance #1 — narration said "he poured ketchup"
  but the rendered panel showed the protagonist eating, not the partner.
  The author was defaulting to protagonist regardless of who the line
  was about.
- Preflight 88d98126 grievance #2 — the protagonist's face was the same
  concerned/sad expression in every panel even as the narration
  escalated proud → outraged → defeated.

These tests pin the schema contract (subject + emotion are now required
per-beat fields) and the cross-module integration (subject=partner →
``refined_visual`` leads with the partner description, not the
protagonist).

We do NOT exercise the real LLM here — both the author LLM and the
refiner LLM are mocked. The point is the schema contract + downstream
plumbing, not the model's English understanding.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT, FakeBeat  # noqa: F401 — sets sys.path


# Pre-stub pipeline.images so prompts.py's import resolves without
# pulling torch — same pattern as test_pipeline_prompts.py:31.
if "pipeline.images.images" not in sys.modules:
    _stub = types.ModuleType("pipeline.images.images")
    _stub.lint_prompt = lambda scene: []
    _stub.strip_text_bait = lambda scene: (scene, [])
    _stub._PIPE = None
    _stub._IP_ADAPTER_LOADED = False
    _stub._FLUX_PIPE = None
    _stub._ZIMAGE_PIPE = None

    def _stub_reset_image_state():  # noqa: D401
        _stub._PIPE = None
        _stub._IP_ADAPTER_LOADED = False
        _stub._FLUX_PIPE = None
        _stub._ZIMAGE_PIPE = None

    _stub.reset_image_state = _stub_reset_image_state
    sys.modules["pipeline.images.images"] = _stub


from pipeline.llm import prompts as pr  # noqa: E402
from pipeline.images import prompt_refiner as refiner  # noqa: E402


class BeatResponseSchemaTest(unittest.TestCase):
    """Pin that subject + emotion are required fields on every beat —
    not optional, not in additionalProperties.

    Why a schema-shape test rather than just an integration test: Azure's
    structured-output mode enforces ``required`` at the token-generation
    level. A regression that drops subject or emotion from
    ``required`` would let the LLM emit beats without these fields,
    silently re-enabling the 88d98126 grievances.
    """

    def test_required_includes_subject_and_emotion(self):
        item_schema = pr._BEAT_RESPONSE_SCHEMA["properties"]["beats"]["items"]
        required = item_schema["required"]
        self.assertIn("subject", required,
                      "subject MUST be required per beat (Rule 18)")
        self.assertIn("emotion", required,
                      "emotion MUST be required per beat (Rule 19)")
        # Pre-existing required fields must stay required — regression
        # guard against accidental schema rewrites.
        self.assertIn("key_visual", required)
        self.assertIn("scene", required)
        self.assertIn("narration_line", required)

    def test_emotion_is_enum_of_14_tokens(self):
        item_schema = pr._BEAT_RESPONSE_SCHEMA["properties"]["beats"]["items"]
        emotion_schema = item_schema["properties"]["emotion"]
        self.assertEqual(emotion_schema["type"], "string")
        self.assertIn("enum", emotion_schema,
                      "emotion MUST be enum-constrained so Azure strict "
                      "mode rejects synonyms like 'livid' / 'infuriated'")
        self.assertEqual(len(emotion_schema["enum"]), 14)
        # Spot-check that the load-bearing tokens for the 88d98126
        # critique are present.
        for required_token in ("proud", "outraged", "defeated", "neutral"):
            self.assertIn(required_token, emotion_schema["enum"])

    def test_allowed_emotions_constant_mirrors_enum(self):
        item_schema = pr._BEAT_RESPONSE_SCHEMA["properties"]["beats"]["items"]
        emotion_schema = item_schema["properties"]["emotion"]
        self.assertEqual(
            list(pr.ALLOWED_EMOTIONS), emotion_schema["enum"],
            "ALLOWED_EMOTIONS export must be the same list as the schema "
            "enum — divergence here means consumers (refiner, tests) see "
            "a different vocabulary than what the LLM is allowed to emit."
        )

    def test_subject_is_free_form_string(self):
        # subject accepts "secondary_<name>" with arbitrary suffix — an
        # enum would over-constrain. The refiner's _subject_lead does
        # the routing; the schema only enforces type=string.
        item_schema = pr._BEAT_RESPONSE_SCHEMA["properties"]["beats"]["items"]
        subject_schema = item_schema["properties"]["subject"]
        self.assertEqual(subject_schema["type"], "string")
        self.assertNotIn("enum", subject_schema)


class ValidateAndCleanSubjectEmotionTest(unittest.TestCase):
    """``_validate_and_clean`` must thread subject + emotion through to
    the cleaned output, with per-beat fallback to protagonist/neutral on
    missing or invalid tokens (per
    feedback_silent_fallback_unshippable_output: per-beat fallback, not
    whole-batch, with a warning printed)."""

    def _beats(self, n):
        return [FakeBeat(text=f"beat {i}", start=i * 1.5, end=(i + 1) * 1.5)
                for i in range(n)]

    def test_subject_and_emotion_threaded_through_to_cleaned(self):
        # Use shot-led key_visual so the verb-led validator doesn't fire.
        # Disable richness gate is not needed here — _validate_and_clean
        # itself respects YTFACTORY_DISABLE_RICHNESS_GATE for the verb-
        # led + composition-fingerprint validators, which is what we
        # need.
        with patch.dict("os.environ", {"YTFACTORY_DISABLE_RICHNESS_GATE": "1"}):
            cleaned = pr._validate_and_clean(
                [{
                    "key_visual": "kv0",
                    "scene": "s0",
                    "subject": "partner",
                    "emotion": "outraged",
                }],
                self._beats(1),
                opening_directives=None,
            )
        self.assertEqual(cleaned[0]["subject"], "partner")
        self.assertEqual(cleaned[0]["emotion"], "outraged")

    def test_missing_subject_falls_back_to_protagonist_with_warning(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch.dict("os.environ", {"YTFACTORY_DISABLE_RICHNESS_GATE": "1"}), \
             redirect_stdout(buf):
            cleaned = pr._validate_and_clean(
                [{"key_visual": "kv0", "scene": "s0", "emotion": "proud"}],
                self._beats(1),
                opening_directives=None,
            )
        self.assertEqual(cleaned[0]["subject"], "protagonist")
        # emotion was present so it's preserved.
        self.assertEqual(cleaned[0]["emotion"], "proud")
        # Operator-visible warning so the gap surfaces in cloud logs.
        self.assertIn("missing 'subject'", buf.getvalue())

    def test_invalid_subject_falls_back_to_protagonist(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch.dict("os.environ", {"YTFACTORY_DISABLE_RICHNESS_GATE": "1"}), \
             redirect_stdout(buf):
            cleaned = pr._validate_and_clean(
                [{
                    "key_visual": "kv0", "scene": "s0",
                    "subject": "the universe itself",  # not a valid token
                    "emotion": "neutral",
                }],
                self._beats(1),
                opening_directives=None,
            )
        self.assertEqual(cleaned[0]["subject"], "protagonist")
        self.assertIn("invalid subject token", buf.getvalue())

    def test_missing_emotion_falls_back_to_neutral(self):
        with patch.dict("os.environ", {"YTFACTORY_DISABLE_RICHNESS_GATE": "1"}):
            cleaned = pr._validate_and_clean(
                [{
                    "key_visual": "kv0", "scene": "s0",
                    "subject": "protagonist",
                }],
                self._beats(1),
                opening_directives=None,
            )
        self.assertEqual(cleaned[0]["emotion"], "neutral")

    def test_invalid_emotion_token_falls_back_to_neutral(self):
        with patch.dict("os.environ", {"YTFACTORY_DISABLE_RICHNESS_GATE": "1"}):
            cleaned = pr._validate_and_clean(
                [{
                    "key_visual": "kv0", "scene": "s0",
                    "subject": "protagonist",
                    "emotion": "livid",  # synonym for outraged, not in enum
                }],
                self._beats(1),
                opening_directives=None,
            )
        self.assertEqual(cleaned[0]["emotion"], "neutral")

    def test_secondary_role_subject_accepted_verbatim(self):
        # secondary_<role> tokens must pass through unchanged — the
        # routing happens at refine time, not validate time.
        with patch.dict("os.environ", {"YTFACTORY_DISABLE_RICHNESS_GATE": "1"}):
            cleaned = pr._validate_and_clean(
                [{
                    "key_visual": "kv0", "scene": "s0",
                    "subject": "secondary_amelia",
                    "emotion": "amused",
                }],
                self._beats(1),
                opening_directives=None,
            )
        self.assertEqual(cleaned[0]["subject"], "secondary_amelia")
        self.assertEqual(cleaned[0]["emotion"], "amused")

    def test_scene_subject_accepted(self):
        with patch.dict("os.environ", {"YTFACTORY_DISABLE_RICHNESS_GATE": "1"}):
            cleaned = pr._validate_and_clean(
                [{
                    "key_visual": "kv0", "scene": "s0",
                    "subject": "scene",
                    "emotion": "tense",
                }],
                self._beats(1),
                opening_directives=None,
            )
        self.assertEqual(cleaned[0]["subject"], "scene")


class SubjectPartnerDrivesRefinedVisualLeadTest(unittest.TestCase):
    """End-to-end pin: a beat with subject=partner causes the refined
    visual to START with a clause centred on the partner — NOT the
    protagonist. This is the load-bearing guarantee against grievance #1
    of the 88d98126 preflight critique.
    """

    def test_partner_subject_with_supporting_cast_leads_refined_visual(self):
        # Pre-stub strip_text_bait so it passes the visuals through
        # unchanged — we're testing the lead injection, not the lint.
        from pipeline.images import images as _images_mod
        with patch.object(_images_mod, "strip_text_bait", lambda s: (s, [])):
            # Mock LLM returns a refined_visual centred on the
            # protagonist (the typical LLM drift). The deterministic
            # post-LLM injection should prepend a partner clause anyway.
            def fake_llm(_prompt, *, output_json=False, **_kw):
                return {"refined_beats": [{
                    "refined_visual": (
                        "medium shot of a woman watching her partner "
                        "eating from a bowl"
                    ),
                    "refined_scene": (
                        "no readable text in image. warm kitchen, soft "
                        "overhead pendant light, butcher-block counter"
                    ),
                    "style_block": "Style: hand-drawn 2D. Mood: tense.",
                }]}

            beats = [{
                "key_visual": "medium shot of the partner pouring ketchup",
                "scene": "kitchen counter, evening pendant light",
                "subject": "partner",
                "emotion": "outraged",
                "narration_line": "he poured ketchup over my stew",
            }]
            supporting = [{
                "name": "partner",
                "aliases": ["husband", "my partner"],
                "description": (
                    "a 32-year-old man with short brown hair, slim "
                    "build, wearing a charcoal grey hoodie and dark jeans"
                ),
            }]

            out = refiner.refine_prompts_batch(
                beats,
                era_anchor_prefix=None,
                character_description="a 30yo woman with long black hair",
                style="hand-drawn 2D",
                mood="tense",
                supporting=supporting,
                llm_call=fake_llm,
            )

        self.assertEqual(len(out), 1)
        rv = out[0]["refined_visual"]
        # Lead clause MUST come from supporting[0]'s description —
        # without this, z-turbo would render the protagonist (since
        # character_description prepends at compose time and the
        # LLM's refined_visual centred on the woman).
        self.assertTrue(
            rv.lower().startswith("medium shot of a 32-year-old man"),
            f"refined_visual must lead with the partner description; got: {rv!r}",
        )
        # The subject_lead diagnostic field must also surface the lead.
        self.assertIn("32-year-old man", out[0]["subject_lead"])

    def test_partner_subject_no_supporting_uses_generic_age_gender_fallback(self):
        """Per the spec: when ``supporting`` is empty/None, fall back to
        a generic age/gender description mined from the narration line
        ("a 32-year-old man" rather than dropping the beat)."""
        from pipeline.images import images as _images_mod
        with patch.object(_images_mod, "strip_text_bait", lambda s: (s, [])):
            def fake_llm(_prompt, *, output_json=False, **_kw):
                return {"refined_beats": [{
                    "refined_visual": "medium shot, kitchen at evening",
                    "refined_scene": (
                        "no readable text in image. warm kitchen counter, "
                        "pendant light"
                    ),
                    "style_block": "Style: x. Mood: y.",
                }]}

            beats = [{
                "key_visual": "kv",
                "scene": "kitchen",
                "subject": "partner",
                "emotion": "outraged",
                "narration_line": "my partner (32M) poured ketchup over the stew",
            }]
            out = refiner.refine_prompts_batch(
                beats,
                era_anchor_prefix=None,
                character_description=None,
                style="x",
                mood="y",
                supporting=None,  # no supporting cast
                llm_call=fake_llm,
            )
        rv = out[0]["refined_visual"]
        # The "(32M)" pattern must resolve to "32-year-old man".
        self.assertIn("32-year-old man", rv,
                      f"expected age/gender fallback in lead; got: {rv!r}")

    def test_protagonist_subject_does_not_prepend_a_lead_clause(self):
        """For subject=protagonist, the refined_visual must NOT gain a
        subject lead — the character_description that the renderer
        prepends at compose time already locks the protagonist."""
        from pipeline.images import images as _images_mod
        with patch.object(_images_mod, "strip_text_bait", lambda s: (s, [])):
            def fake_llm(_prompt, *, output_json=False, **_kw):
                return {"refined_beats": [{
                    "refined_visual": "medium shot of the character cooking",
                    "refined_scene": (
                        "no readable text in image. kitchen counter, "
                        "soft pendant light"
                    ),
                    "style_block": "Style: x. Mood: y.",
                }]}
            beats = [{
                "key_visual": "kv", "scene": "kitchen",
                "subject": "protagonist",
                "emotion": "proud",
                "narration_line": "I cooked the stew for hours",
            }]
            out = refiner.refine_prompts_batch(
                beats,
                era_anchor_prefix=None,
                character_description="protagonist desc",
                style="x", mood="y",
                supporting=None,
                llm_call=fake_llm,
            )
        self.assertEqual(out[0]["subject_lead"], "",
                         "protagonist beats must not prepend a subject lead")
        rv = out[0]["refined_visual"]
        # The LLM's refined_visual must lead — no clause prepended.
        self.assertTrue(rv.startswith("medium shot of the character cooking"),
                        f"expected LLM's refined_visual as the lead; got: {rv!r}")


class SupportingRoleTokensSurfacedInPromptTest(unittest.TestCase):
    """The author's user prompt must list the concrete
    ``secondary_<role>`` tokens it's allowed to emit — without this list
    the LLM either invents tokens that downstream can't route or
    collapses to "secondary_other"."""

    def test_supporting_role_tokens_listed_when_supporting_provided(self):
        beats = [FakeBeat("Amelia said it was fine.", 0, 1.5)]
        prompt = pr._build_user_prompt(
            narration="Amelia said it was fine.",
            beats=beats,
            source_story="source",
            cast_narrator_desc="brown hair narrator",
            cast_default_emotion=None,
            style_prefix="pastel watercolor style",
            opening_directives=None,
            supporting=[
                {"name": "Amelia", "description": "tall sister with red hair"},
                {"name": "my MIL", "description": "elderly woman with silver hair"},
            ],
        )
        self.assertIn("secondary_amelia", prompt)
        self.assertIn("secondary_my_mil", prompt)
        self.assertIn("ALLOWED secondary subject tokens", prompt)

    def test_supporting_role_tokens_absent_when_supporting_empty(self):
        beats = [FakeBeat("I cooked alone.", 0, 1.5)]
        prompt = pr._build_user_prompt(
            narration="I cooked alone.",
            beats=beats,
            source_story="source",
            cast_narrator_desc="brown hair narrator",
            cast_default_emotion=None,
            style_prefix="pastel watercolor style",
            opening_directives=None,
            supporting=None,
        )
        self.assertNotIn("ALLOWED secondary subject tokens", prompt)


if __name__ == "__main__":
    unittest.main()
