"""Tests for the refined-prompt integration in
``pipeline.render.visualize.ai_beat_slideshow``.

Added 2026-05-14 alongside the prompt-refiner pre-step (originally
FLUX.2 [klein]; rewritten 2026-05-23 for Z-Image-Turbo).
Exercises:

* :func:`_load_prompts_json` — best-effort prompts.json loading that
  tolerates missing files, malformed JSON, and non-list payloads
  without raising (so engine renders never fail because of a bad
  cache file).
* :func:`_resolve_prompt_for_beat` — the per-beat assembly that
  chooses between (a) refined-mode build_full_prompt, (b)
  legacy-mode build_full_prompt, or (c) bare Segment.text fallback.

The full ``AiBeatSlideshow.produce`` flow is exercised by the engine
goldens (``tests/render/test_short_engine_golden.py``) — these tests
target the two new helpers in isolation so the per-beat fallback
contract is pinned.

Isolation note: we do NOT poison ``sys.modules["pipeline.images.images"]``
— it leaks to other test files that need the real
``build_full_prompt``. Instead we patch the real module's
``build_full_prompt`` per-test via ``unittest.mock.patch.object``, with
a stub that emits markers we can assert against.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path

from pipeline.images import images as _images_mod
from pipeline.render.visualize import ai_beat_slideshow as abs_mod


def _marker_build_full_prompt(  # noqa: PLR0913 — mirrors real signature
    *,
    style_prefix: str,
    character_description: str | None,
    key_visual: str | None,
    scene: str,
    era_anchor_prefix: str | None = None,
    key_visual_weight: float = 1.4,
    weighted: bool = True,
    refined_visual: str | None = None,
    refined_scene: str | None = None,
    style_block: str | None = None,
    subject: str | None = None,  # F32 — must mirror real signature
) -> str:
    """Test stub: emit a deterministic marker string so each test can
    distinguish the refined vs legacy assembly path. Returned in place
    of the real ``build_full_prompt`` via ``patch.object``."""
    if refined_visual and refined_scene and style_block:
        return f"REFINED|{refined_visual}|{refined_scene}|{style_block}"
    return f"LEGACY|kv={key_visual}|scene={scene}|style={style_prefix}"


def _install_marker(test_self):
    """Patch ``pipeline.images.images.build_full_prompt`` for the
    duration of ``test_self`` with the marker stub above. Auto-restores
    via addCleanup."""
    patcher = patch.object(_images_mod, "build_full_prompt", _marker_build_full_prompt)
    patcher.start()
    test_self.addCleanup(patcher.stop)


@contextmanager
def _patch_env(name: str, value: str | None):
    prev = os.environ.get(name)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev


# ---------------------------------------------------------------------------
# _load_prompts_json


class LoadPromptsJsonTest(unittest.TestCase):
    def test_none_path_returns_none(self):
        self.assertIsNone(abs_mod._load_prompts_json(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(abs_mod._load_prompts_json(""))

    def test_missing_file_returns_none_without_raising(self):
        # Engine must not crash because prompts.json was never written
        # (heuristic-prompt fallback or test render with no cache).
        self.assertIsNone(abs_mod._load_prompts_json("/nonexistent/path/x.json"))

    def test_malformed_json_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{ this is not valid JSON")
            self.assertIsNone(abs_mod._load_prompts_json(p))

    def test_non_list_payload_returns_none(self):
        # prompts.json contract: list of beat dicts. Anything else is
        # treated as unusable.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "obj.json"
            p.write_text(json.dumps({"key_visual": "x"}))
            self.assertIsNone(abs_mod._load_prompts_json(p))

    def test_valid_list_loads_and_returns(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "good.json"
            payload = [
                {"key_visual": "a", "scene": "b"},
                {"key_visual": "c", "scene": "d"},
            ]
            p.write_text(json.dumps(payload))
            out = abs_mod._load_prompts_json(p)
            self.assertEqual(out, payload)


# ---------------------------------------------------------------------------
# _resolve_prompt_for_beat


class ResolvePromptForBeatTest(unittest.TestCase):
    """The three-priority fallback ladder must be observable from
    outside via the returned string. The marker stub above encodes
    path identity into the return value, so each test asserts against
    that marker."""

    def setUp(self):
        # Install the marker stub for this test. Auto-restores so the
        # real build_full_prompt is intact when other test files run.
        _install_marker(self)

    def test_no_beat_dict_returns_bare_fallback_text(self):
        out = abs_mod._resolve_prompt_for_beat(
            beat=None,
            fallback_text="raw segment text",
            style_prefix="style",
            character_description="char",
            era_anchor_prefix="era",
            mood=None,
        )
        # No build_full_prompt path → bare text.
        self.assertEqual(out, "raw segment text")

    def test_legacy_beat_no_refined_fields_uses_legacy_assembly(self):
        beat = {"key_visual": "kv", "scene": "sc"}
        with _patch_env("YTFACTORY_PROMPT_REFINER", None):
            out = abs_mod._resolve_prompt_for_beat(
                beat=beat,
                fallback_text="ignored",
                style_prefix="cartoon",
                character_description="adult",
                era_anchor_prefix="ERA",
                mood=None,
            )
        # Legacy path: stub returns "LEGACY|kv=...|scene=...|style=..."
        self.assertIn("LEGACY", out)
        self.assertIn("kv=kv", out)
        self.assertIn("scene=sc", out)
        self.assertIn("style=cartoon", out)

    def test_beat_with_refined_fields_flag_on_uses_refined_assembly(self):
        # Construct a beat whose refined_input_hash matches the
        # current-context hash (so the render-time gate passes).
        from pipeline.images.prompt_refiner import compute_input_hash, REFINER_VERSION
        beat = {
            "key_visual": "kv",
            "scene": "sc",
            "refined_visual": "RV",
            "refined_scene": "no readable text in image. RS",
            "style_block": "Style: a. Mood: b.",
            "refined_version": REFINER_VERSION,
        }
        beat["refined_input_hash"] = compute_input_hash(
            beat=beat,
            era_anchor_prefix="ERA",
            character_description="adult",
            style="cartoon",
            mood="dramatic",
        )
        with _patch_env("YTFACTORY_PROMPT_REFINER", "1"):
            out = abs_mod._resolve_prompt_for_beat(
                beat=beat,
                fallback_text="ignored",
                style_prefix="cartoon",
                character_description="adult",
                era_anchor_prefix="ERA",
                mood="dramatic",
            )
        # Refined path: stub returns "REFINED|RV|...|..."
        self.assertIn("REFINED", out)
        self.assertIn("RV", out)
        self.assertIn("no readable text in image. RS", out)
        self.assertIn("Style: a. Mood: b.", out)

    def test_beat_with_refined_fields_flag_off_falls_back_to_legacy(self):
        # Same beat as the test above, but with the env flag OFF — the
        # kill switch must trip and we use the legacy assembly even
        # though refined fields are cached.
        from pipeline.images.prompt_refiner import compute_input_hash, REFINER_VERSION
        beat = {
            "key_visual": "kv",
            "scene": "sc",
            "refined_visual": "RV",
            "refined_scene": "no readable text in image. RS",
            "style_block": "Style: a. Mood: b.",
            "refined_version": REFINER_VERSION,
        }
        beat["refined_input_hash"] = compute_input_hash(
            beat=beat,
            era_anchor_prefix="ERA",
            character_description="adult",
            style="cartoon",
            mood="dramatic",
        )
        with _patch_env("YTFACTORY_PROMPT_REFINER", "0"):
            out = abs_mod._resolve_prompt_for_beat(
                beat=beat,
                fallback_text="ignored",
                style_prefix="cartoon",
                character_description="adult",
                era_anchor_prefix="ERA",
                mood="dramatic",
            )
        # Legacy path despite refined fields being present.
        self.assertIn("LEGACY", out)
        self.assertNotIn("REFINED", out)

    def test_critic_patched_scene_drops_refined_via_hash_mismatch(self):
        """When critic patches beat['scene'], the input hash no longer
        matches what the refiner ran with. The render-time gate detects
        the drift via hash mismatch and falls back to legacy. Without
        this, the render would use stale refined_* fields for a freshly
        patched scene → bug class the rubber-duck explicitly flagged."""
        from pipeline.images.prompt_refiner import compute_input_hash, REFINER_VERSION
        beat = {
            "key_visual": "kv",
            "scene": "ORIGINAL",
            "refined_visual": "RV",
            "refined_scene": "no readable text in image. RS",
            "style_block": "Style: a. Mood: b.",
            "refined_version": REFINER_VERSION,
        }
        # Hash was computed when scene="ORIGINAL"
        beat["refined_input_hash"] = compute_input_hash(
            beat={"key_visual": "kv", "scene": "ORIGINAL"},
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        # Critic now patches scene in-place.
        beat["scene"] = "CRITIC PATCHED THIS"
        with _patch_env("YTFACTORY_PROMPT_REFINER", "1"):
            out = abs_mod._resolve_prompt_for_beat(
                beat=beat,
                fallback_text="ignored",
                style_prefix="",
                character_description=None,
                era_anchor_prefix=None,
                mood=None,
            )
        # Hash mismatch → legacy path with the NEW patched scene.
        self.assertIn("LEGACY", out)
        self.assertIn("CRITIC PATCHED THIS", out)
        self.assertNotIn("REFINED", out)

    def test_beat_with_empty_scene_uses_fallback_text(self):
        # If a beat dict has an empty scene field (which shouldn't
        # happen but defensive), we use seg.text so the prompt isn't
        # an empty string.
        beat = {"key_visual": "kv", "scene": ""}
        out = abs_mod._resolve_prompt_for_beat(
            beat=beat,
            fallback_text="seg fallback",
            style_prefix="style",
            character_description=None,
            era_anchor_prefix=None,
            mood=None,
        )
        # Legacy path with the fallback as scene.
        self.assertIn("LEGACY", out)
        self.assertIn("scene=seg fallback", out)


# ---------------------------------------------------------------------------
# AiBeatSlideshow.produce — minimum integration coverage so the
# spec.extra plumbing + per-beat dispatch into _resolve_prompt_for_beat
# is pinned. _generate_image and _stitch_images are patched to avoid
# touching ffmpeg or the cloud image service.


class _FakeSeg:
    def __init__(self, text, start_s=0.0, end_s=1.0):
        self.text = text
        self.start_s = start_s
        self.end_s = end_s


class _FakeSpec:
    """Minimal RenderSpec stand-in. AiBeatSlideshow only reads a handful
    of fields off it, so the real RenderSpec dataclass is overkill for
    these tests."""

    def __init__(self, *, extra=None):
        self.extra = extra or {}
        self.output_resolution = (1080, 1920)
        self.output_fps = 30


class AiBeatSlideshowProduceIntegrationTest(unittest.TestCase):
    """End-to-end exercise of ``AiBeatSlideshow.produce`` covering the
    new spec.extra plumbing + per-beat dispatch. Both ``_generate_image``
    and ``_stitch_images`` are stubbed — the test asserts on the prompts
    that get handed to ``_generate_image``, not on actual mp4 output.
    """

    def setUp(self):
        # build_full_prompt → marker stub so we can assert refined vs
        # legacy path from the captured prompt strings.
        _install_marker(self)

    def _run_produce(self, *, extra, timeline):
        """Drive AiBeatSlideshow.produce() with the generate + stitch
        functions patched. Returns a list of (prompt, style_prefix)
        tuples handed to ``_generate_image``, in beat order. Capturing
        BOTH kwargs lets us assert the no-double-styling contract — the
        legacy style_prefix must NOT be appended after build_full_prompt
        already inlined the style block."""
        captured: list[tuple[str, str]] = []

        def _fake_generate(*, prompt, style_prefix="", **_kwargs):
            captured.append((prompt, style_prefix))
            return None

        slideshow = abs_mod.AiBeatSlideshow()
        spec = _FakeSpec(extra=extra)
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            with patch("pipeline.images.images.generate", side_effect=_fake_generate), \
                 patch.object(slideshow, "_stitch_images", return_value=None), \
                 patch.object(abs_mod, "probe_duration", return_value=10.0):
                slideshow.produce(spec, timeline, work_dir)
        return captured

    def test_produce_no_prompts_path_uses_seg_text_for_every_beat(self):
        # No prompts.json provided → legacy bare-Segment.text path.
        timeline = [_FakeSeg("beat one text"), _FakeSeg("beat two text")]
        captured = self._run_produce(
            extra={"image_style_prefix": "cartoon"},
            timeline=timeline,
        )
        # _resolve_prompt_for_beat returns the bare fallback_text when
        # beat=None (no custom_prompts loaded). The configured style is
        # passed through to generate so it can be appended.
        self.assertEqual(captured[0], ("beat one text", "cartoon"))
        self.assertEqual(captured[1], ("beat two text", "cartoon"))

    def test_produce_with_legacy_prompts_json_uses_build_full_prompt_legacy_path(self):
        # prompts.json present with no refined fields → legacy assembly.
        timeline = [_FakeSeg("ignored seg text")]
        with tempfile.TemporaryDirectory() as td:
            prompts_path = Path(td) / "prompts.json"
            prompts_path.write_text(json.dumps([
                {"key_visual": "phone", "scene": "the character holding the phone"},
            ]))
            # Run inside an env with the flag OFF (kill switch) — even
            # if the beat had refined fields, they would be ignored.
            with _patch_env("YTFACTORY_PROMPT_REFINER", None):
                captured = self._run_produce(
                    extra={
                        "prompts_path": str(prompts_path),
                        "image_style_prefix": "cartoon",
                        "character_description": "adult",
                        "era_anchor_prefix": "[ERA: 2020s]",
                        "mood": "tense",
                    },
                    timeline=timeline,
                )
        self.assertEqual(len(captured), 1)
        prompt, style_kwarg = captured[0]
        self.assertIn("LEGACY", prompt)
        self.assertIn("kv=phone", prompt)
        self.assertIn("scene=the character holding the phone", prompt)
        self.assertIn("style=cartoon", prompt)
        # Critical: style_prefix kwarg must be EMPTY when beat is
        # present — otherwise generate() appends "cartoon" AGAIN at the
        # end of an already-styled prompt (rubber-duck #2). The legacy
        # build_full_prompt assembly already inlines style_prefix.
        self.assertEqual(
            style_kwarg, "",
            "style_prefix must be empty when build_full_prompt assembled the prompt",
        )

    def test_produce_with_refined_fields_flag_on_uses_refined_assembly(self):
        # End-to-end: refined fields are cached + flag is on → refined
        # assembly fires for that beat.
        from pipeline.images.prompt_refiner import compute_input_hash, REFINER_VERSION
        beat = {
            "key_visual": "phone",
            "scene": "holding phone",
            "refined_visual": "phone with glowing screen",
            "refined_scene": "no readable text in image. extreme close-up, warm light, fingers gripping the device",
            "style_block": "Style: cartoon. Mood: tense.",
            "refined_version": REFINER_VERSION,
        }
        beat["refined_input_hash"] = compute_input_hash(
            beat=beat,
            era_anchor_prefix="[ERA: 2020s]",
            character_description="adult",
            style="cartoon",
            mood="tense",
        )
        with tempfile.TemporaryDirectory() as td:
            prompts_path = Path(td) / "prompts.json"
            prompts_path.write_text(json.dumps([beat]))
            with _patch_env("YTFACTORY_PROMPT_REFINER", "1"):
                captured = self._run_produce(
                    extra={
                        "prompts_path": str(prompts_path),
                        "image_style_prefix": "cartoon",
                        "character_description": "adult",
                        "era_anchor_prefix": "[ERA: 2020s]",
                        "mood": "tense",
                    },
                    timeline=[_FakeSeg("ignored")],
                )
        self.assertEqual(len(captured), 1)
        prompt, style_kwarg = captured[0]
        self.assertIn("REFINED", prompt)
        self.assertIn("phone with glowing screen", prompt)
        # Style block is INSIDE the refined prompt — must NOT be also
        # appended via style_prefix kwarg or the LLM sees two style
        # specifications competing.
        self.assertEqual(
            style_kwarg, "",
            "style_prefix must be empty in refined mode — style_block is already inlined",
        )


# ---------------------------------------------------------------------------
# scene_anchor wiring — added 2026-05-24 with O37/O40.
#
# The shorts path reads ``spec.extra["default_scene_anchor"]`` and threads
# it into ``_resolve_prompt_for_beat`` → ``refined_fields_for_render``.
# When set, the gate's hash check must include scene_anchor; when the
# scene_anchor changes between author-time and render-time, the cached
# refined fields auto-invalidate (mirror of the critic-patch-scene
# regression test above).


class ShortsScenseAnchorWiringTest(unittest.TestCase):
    """Pin that spec.extra['default_scene_anchor'] reaches the refined
    gate on the SHORTS path. If a future refactor drops the wire,
    long-form gets the anchor but shorts silently falls back to the
    no-anchor refined fields, defeating the symmetric-channel contract.
    """

    def setUp(self):
        # Use the marker-stubbed build_full_prompt so we can distinguish
        # the refined-mode vs legacy-mode branch from the returned
        # string (same setup as ResolvePromptForBeatTest).
        _install_marker(self)

    def test_resolve_prompt_passes_scene_anchor_to_render_gate(self):
        """_resolve_prompt_for_beat MUST thread scene_anchor into the
        render-time hash check. We pin this by computing the cached
        beat's hash WITH the anchor; without the wire, the gate would
        recompute WITHOUT the anchor → mismatch → legacy fallback.
        """
        from pipeline.images.prompt_refiner import compute_input_hash, REFINER_VERSION

        anchor = "inside an airplane cabin, dim ambient lighting"
        beat = {
            "key_visual": "kv",
            "scene": "sc",
            "refined_visual": "RV_with_anchor",
            "refined_scene": "no readable text in image. RS",
            "style_block": "Style: a. Mood: b.",
            "refined_version": REFINER_VERSION,
        }
        # Cache the hash WITH the anchor present (mirrors what the
        # refiner did at author-time).
        beat["refined_input_hash"] = compute_input_hash(
            beat=beat,
            era_anchor_prefix="ERA",
            character_description="adult",
            style="cartoon",
            mood="dramatic",
            scene_anchor=anchor,
        )

        with _patch_env("YTFACTORY_PROMPT_REFINER", "1"):
            out = abs_mod._resolve_prompt_for_beat(
                beat=beat,
                fallback_text="ignored",
                style_prefix="cartoon",
                character_description="adult",
                era_anchor_prefix="ERA",
                mood="dramatic",
                scene_anchor=anchor,
            )
        # Anchor matched on both sides → refined path wins.
        self.assertIn("REFINED", out, msg=(
            "scene_anchor not threaded through _resolve_prompt_for_beat. "
            "Render-time hash check sees no scene_anchor while the cache "
            "was computed WITH it, so the gate mismatches and falls back "
            "to legacy — defeating the O40 channel-anchor contract."
        ))
        self.assertIn("RV_with_anchor", out)

    def test_anchor_mismatch_drops_to_legacy(self):
        """When the channel anchor changes between author-time and
        render-time (a YAML edit), cached refined fields MUST be
        invalidated by the hash check — same shape as the critic-patch
        regression. Without the anchor in the hash, this test would
        wrongly assert REFINED."""
        from pipeline.images.prompt_refiner import compute_input_hash, REFINER_VERSION

        cached_anchor = "inside an airplane cabin"
        new_anchor = "outdoor mountain pass at dusk"
        beat = {
            "key_visual": "kv",
            "scene": "sc",
            "refined_visual": "RV",
            "refined_scene": "no readable text in image. RS",
            "style_block": "Style: a. Mood: b.",
            "refined_version": REFINER_VERSION,
        }
        beat["refined_input_hash"] = compute_input_hash(
            beat=beat,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            scene_anchor=cached_anchor,
        )
        with _patch_env("YTFACTORY_PROMPT_REFINER", "1"):
            out = abs_mod._resolve_prompt_for_beat(
                beat=beat,
                fallback_text="bare scene",
                style_prefix="",
                character_description=None,
                era_anchor_prefix=None,
                mood=None,
                scene_anchor=new_anchor,  # different from cached
            )
        # Hash mismatch → legacy path with the original key_visual/scene.
        self.assertIn("LEGACY", out)
        self.assertNotIn("REFINED", out)


if __name__ == "__main__":
    unittest.main()
