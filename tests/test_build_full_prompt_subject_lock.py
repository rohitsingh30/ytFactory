"""F32 counter-test — protagonist character_description must be
SKIPPED in refined mode when the per-beat ``subject`` is non-protagonist.

The bug (catalogued as F32 in ``/ai/known-fragility.md``):
``pipeline/images/images.py::build_full_prompt`` used to unconditionally
prepend ``character_description`` (the protagonist's description) in
refined mode. For beats whose authored ``subject`` was ``partner`` or
``secondary_<X>``, the refiner's ``_subject_lead`` correctly led
``refined_visual`` with the non-protagonist subject — but the
unconditional protagonist prepend then dominated Z-Image-Turbo's
left-weighted encoder and collapsed the secondary character into a
copy of the protagonist (job 39d1ec2d cast-collapse).

These tests pin the post-2026-05-24 invariants:

- subject="protagonist" / unset → character_description prepends
  (back-compat for pre-v5 caches, and the explicit protagonist case).
- subject="partner" / "secondary_<X>" / "scene" → character_description
  does NOT appear in the final prompt.
- era_anchor_prefix prepends regardless of subject.
- Legacy mode (refined_* not supplied) prepends unconditionally —
  there is no per-beat subject lead to substitute the safety belt.
"""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path


CAST = "a 24-year-old woman, brown hair, yellow t-shirt, hazel eyes"
ERA = "[ERA — modern 2020s American school]"
RV_PROTAG = "the woman watching from across the schoolyard, alert posture"
RV_PARTNER = "medium shot of a 32-year-old man with a beard pouring ketchup"
RV_SECONDARY = "the male gym teacher with a whistle cord, wide-eyed"
RV_SCENE = "an empty schoolyard at dusk, leaves drifting"
RS = "no readable text in image. medium shot, golden-hour rim light, asphalt"
SB = "Style: indie webcomic illustration. Mood: tense morning."


def _build(*, subject, refined_visual=RV_PARTNER):
    from pipeline.images import images as img
    return img.build_full_prompt(
        style_prefix="legacy_style_prefix",
        character_description=CAST,
        key_visual="legacy_kv",
        scene="legacy_scene",
        era_anchor_prefix=ERA,
        refined_visual=refined_visual,
        refined_scene=RS,
        style_block=SB,
        subject=subject,
    )


class SubjectLockSkipsProtagonistDescriptionTest(unittest.TestCase):
    """When subject is non-protagonist, the protagonist's
    character_description must NOT be in the final prompt."""

    def test_subject_partner_skips_character_description(self):
        prompt = _build(subject="partner")
        self.assertNotIn(CAST, prompt)

    def test_subject_secondary_named_skips_character_description(self):
        prompt = _build(subject="secondary_gym_teacher",
                        refined_visual=RV_SECONDARY)
        self.assertNotIn(CAST, prompt)

    def test_subject_scene_skips_character_description(self):
        prompt = _build(subject="scene", refined_visual=RV_SCENE)
        self.assertNotIn(CAST, prompt)

    def test_subject_partner_refined_visual_leads_after_era_anchor(self):
        """With the protagonist prepend gone, refined_visual must be
        the dominant subject (Z-Image-Turbo left-weighted encoder)."""
        prompt = _build(subject="partner")
        self.assertTrue(prompt.startswith(ERA),
                        f"era_anchor must still lead; got {prompt[:80]!r}")
        rv_idx = prompt.index(RV_PARTNER)
        # refined_visual should be in the first ~120 chars after era_anchor.
        self.assertLess(rv_idx, len(ERA) + 50,
                        f"refined_visual should land directly after era_anchor; "
                        f"got prompt: {prompt[:200]!r}")

    def test_subject_case_insensitive(self):
        # PARTNER / Partner / partner should all skip equally.
        for variant in ("PARTNER", "Partner", "partner", " partner "):
            prompt = _build(subject=variant)
            self.assertNotIn(CAST, prompt,
                             f"subject={variant!r} should skip cast prepend")


class ProtagonistSubjectKeepsCharacterDescriptionTest(unittest.TestCase):
    """When subject is the protagonist (or unset), character_description
    must still prepend — the cast lock for the protagonist case is
    unchanged."""

    def test_subject_protagonist_keeps_character_description(self):
        prompt = _build(subject="protagonist", refined_visual=RV_PROTAG)
        self.assertIn(CAST, prompt)

    def test_subject_none_keeps_character_description(self):
        """Pre-v5 cached prompts have no ``subject`` field; backward
        compat requires the legacy prepend behaviour."""
        prompt = _build(subject=None, refined_visual=RV_PROTAG)
        self.assertIn(CAST, prompt)

    def test_subject_empty_string_keeps_character_description(self):
        # Empty string is treated as "unset" — same as None.
        prompt = _build(subject="", refined_visual=RV_PROTAG)
        self.assertIn(CAST, prompt)

    def test_subject_protagonist_character_description_leads_refined_visual(self):
        """Existing post-2026-05-14 invariant (the OTHER existing test
        already pins this for unset subject; pin again for explicit
        protagonist subject)."""
        prompt = _build(subject="protagonist", refined_visual=RV_PROTAG)
        cidx = prompt.index(CAST)
        ridx = prompt.index(RV_PROTAG)
        self.assertLess(cidx, ridx)


class LegacyModeSubjectIgnoredTest(unittest.TestCase):
    """Legacy mode (refined_* not supplied) has no subject_lead in the
    final prompt to substitute for character_description, so the safety
    belt must REMAIN — character_description prepends regardless of
    the subject token."""

    def _legacy(self, *, subject):
        from pipeline.images import images as img
        return img.build_full_prompt(
            style_prefix="legacy_style",
            character_description=CAST,
            key_visual="a coffee cup",
            scene="hands on the cup",
            era_anchor_prefix=ERA,
            refined_visual=None,
            refined_scene=None,
            style_block=None,
            subject=subject,
        )

    def test_legacy_mode_partner_still_prepends_cast(self):
        prompt = self._legacy(subject="partner")
        self.assertIn(CAST, prompt)

    def test_legacy_mode_scene_still_prepends_cast(self):
        prompt = self._legacy(subject="scene")
        self.assertIn(CAST, prompt)


class SubjectKwargBackwardCompatTest(unittest.TestCase):
    """Calling ``build_full_prompt`` without the new ``subject`` kwarg
    must produce byte-identical output to the legacy (subject=None)
    invocation. Old call sites must not see any behaviour change."""

    def test_subject_omitted_equals_subject_none(self):
        from pipeline.images import images as img
        kw = dict(
            style_prefix="cartoon",
            character_description=CAST,
            key_visual="kv",
            scene="scene",
            era_anchor_prefix=ERA,
            refined_visual=RV_PROTAG,
            refined_scene=RS,
            style_block=SB,
        )
        without = img.build_full_prompt(**kw)
        with_none = img.build_full_prompt(**kw, subject=None)
        self.assertEqual(without, with_none)


if __name__ == "__main__":
    unittest.main()
