"""Regression tests for the anti-text safety clause shape.

Background — see
``data/critiques/i-ve-been-flying-for-almost-thirty-hours-and-the-flight-atte-845bdb0d.md``
and ``.claude/skills/diagnose-render/learnings/845bdb0df20e4ba3885ca33c7749e74d.md``.

Between 2026-05 and 2026-05-23, ``pipeline/images/images_cloudrun.py``
PREPENDED a 190-char noun-tag boilerplate to every cloud-Z-Image-Turbo
prompt ("Clean surface, unmarked, blank jersey, smooth fabric, plain
backgrounds, unmarked book covers, unlabeled bottles, …"). The author's
intent was positive-framing-at-the-front for left-weighted distilled
models. The chosen nouns ("blank jersey", "smooth fabric", "unmarked
book covers", "unlabeled bottles") were concrete clothing / fabric /
paper-product nouns strongly attested in image-model training; when
the panel-specific scene tail was short (e.g. "Cloud-filled sky seen
from airplane wing."), the noun-tag prefix dominated the model's
subject selection and the rendered output was a literal floating
product-photo white t-shirt against the requested background. ~45 of
77 panels on the 845bdb0d render rendered as headless floating tees.

These tests pin the rewrite:

1. The safety clause string must NOT contain any of the harmful
   subject-nouns ("blank jersey", "smooth fabric", "unmarked book
   covers", "unlabeled bottles").
2. The clause must be appended as a SUFFIX, not prepended.
3. Idempotence still works against both the new substring and the
   legacy substring (so cached prompts and refiner-emitted prompts
   don't get double-suffixed).

If any of these assertions fail, the floating-t-shirt symptom is
likely to return on the next render.
"""

from __future__ import annotations

import pytest

from pipeline.images.images_cloudrun import (
    ANTI_TEXT_SUFFIX,
    _append_anti_text_suffix,
)


FORBIDDEN_SUBJECT_NOUNS = (
    "blank jersey",
    "smooth fabric",
    "unmarked book covers",
    "unlabeled bottles",
    "Clean surface, unmarked",
)


class AntiTextSuffixShape:
    """Pinned shape — the literal string the wire prompt carries."""

    def test_suffix_string_contains_none_of_the_forbidden_nouns(self) -> None:
        for noun in FORBIDDEN_SUBJECT_NOUNS:
            assert noun not in ANTI_TEXT_SUFFIX, (
                f"ANTI_TEXT_SUFFIX contains the harmful subject-noun "
                f"{noun!r}. This is the 2026-05-23 floating-jersey "
                f"regression — z-image-turbo will treat this as a "
                f"draw-subject. See data/critiques/i-ve-been-flying-"
                f"...-845bdb0d.md."
            )

    def test_suffix_string_is_short_enough_to_not_dominate(self) -> None:
        # The 2026-05 boilerplate was 190 chars. Anything in the 100-180
        # char band still risks dominance on short panel tails. Pin a
        # tight upper bound.
        assert len(ANTI_TEXT_SUFFIX) <= 250, (
            f"ANTI_TEXT_SUFFIX is {len(ANTI_TEXT_SUFFIX)} chars; "
            f"keep it ≤ 250 to avoid dominating short scene tails."
        )

    def test_suffix_string_starts_with_a_verb_not_a_noun_tag(self) -> None:
        # "Render the scene with no…" — first word is a verb. The
        # broken version started with "Clean" as an adjective modifying
        # the noun "surface", parsed as a noun-tag subject.
        first_word = ANTI_TEXT_SUFFIX.split()[0].lower()
        assert first_word in {
            "render", "compose", "produce", "depict", "show", "draw",
            "avoid", "scene", "the", "no",
        }, (
            f"ANTI_TEXT_SUFFIX first word is {first_word!r}; expected "
            f"a verb or scene-anchor word, not a subject-noun-modifier."
        )


class AppendBehavior:
    """``_append_anti_text_suffix`` — append, not prepend."""

    def test_appends_safety_clause_to_end_of_prompt(self) -> None:
        scene = "A woman in a yellow t-shirt sitting on an airplane."
        out = _append_anti_text_suffix(scene, model="z_image_turbo")
        # Scene must come first; safety clause must come AFTER.
        scene_pos = out.find(scene.rstrip("."))
        suffix_pos = out.find(ANTI_TEXT_SUFFIX)
        assert scene_pos == 0, (
            f"_append_anti_text_suffix should leave the scene at the "
            f"start; found scene at position {scene_pos}. Output: "
            f"{out!r}"
        )
        assert suffix_pos > scene_pos, (
            f"_append_anti_text_suffix should append the safety clause "
            f"AFTER the scene; found suffix at {suffix_pos} vs scene "
            f"at {scene_pos}. Output: {out!r}"
        )

    def test_preserves_scene_text_verbatim(self) -> None:
        scene = "Cloud-filled sky seen from airplane wing."
        out = _append_anti_text_suffix(scene, model="z_image_turbo")
        # The scene's content words must survive the wrap.
        for word in ("Cloud-filled", "sky", "airplane", "wing"):
            assert word in out, (
                f"_append_anti_text_suffix dropped the word {word!r} "
                f"from the scene. Output: {out!r}"
            )


class Idempotence:
    """No double-suffix — important for cached prompts and refiner-routed prompts."""

    def test_legacy_substring_skips_reapply(self) -> None:
        # Pre-2026-05-24 cached prompts already have the old prefix
        # baked in. They must NOT be re-suffixed.
        legacy = (
            "Clean surface, unmarked, blank jersey, smooth fabric, "
            "plain backgrounds. A woman in a yellow t-shirt."
        )
        out = _append_anti_text_suffix(legacy, model="z_image_turbo")
        assert out == legacy, (
            f"_append_anti_text_suffix should NOT re-suffix legacy "
            f"cached prompts. Got: {out!r}"
        )

    def test_refiner_substring_skips_reapply(self) -> None:
        # Refiner-emitted refined_scene starts with "no readable text
        # in image." (see pipeline/images/prompt_refiner.py:435). When
        # the long-form path is routed through the refiner, this
        # substring will be present and the suffix must be a no-op.
        refined = (
            "no readable text in image. wide shot, soft lighting, "
            "woman gazes through airplane window at dawn."
        )
        out = _append_anti_text_suffix(refined, model="z_image_turbo")
        assert out == refined, (
            f"_append_anti_text_suffix should NOT re-suffix refiner-"
            f"emitted prompts. Got: {out!r}"
        )

    def test_already_suffixed_prompt_skips_reapply(self) -> None:
        # Apply twice; should be no-op the second time.
        scene = "A woman in a yellow t-shirt sitting on an airplane."
        once = _append_anti_text_suffix(scene, model="z_image_turbo")
        twice = _append_anti_text_suffix(once, model="z_image_turbo")
        assert twice == once, (
            f"_append_anti_text_suffix should be idempotent. "
            f"once={once!r}; twice={twice!r}"
        )

    def test_empty_prompt_returns_empty(self) -> None:
        assert _append_anti_text_suffix("", model="z_image_turbo") == ""


# ----- pytest collection helpers ---------------------------------------

def _instantiate(cls):
    """Pytest collects only top-level test_* functions. Convert each
    inner method to a top-level function so pytest discovers them."""
    instance = cls()
    for name in dir(instance):
        if name.startswith("test_"):
            method = getattr(instance, name)
            wrapper_name = f"test_{cls.__name__.lower()}__{name[len('test_'):]}"
            wrapper = (lambda m=method: m)
            globals()[wrapper_name] = (lambda m=method: m())


_instantiate(AntiTextSuffixShape)
_instantiate(AppendBehavior)
_instantiate(Idempotence)
