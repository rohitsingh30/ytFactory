"""Pin the 2026-05-15 fail-loud-fallback audit.

Background
----------

The 2026-05-15 silent-fallback audit identified five sites in the
render pipeline that swallowed BLE001 exceptions and produced
visibly-broken artifacts:

1. ``pipeline/render/visualize/_fallback.py::_solid_color`` — produced
   solid-color (#141414) stand-ins that shipped 26-min black mp4s on
   every cosmosdecoded long-form render (jobs ce309c80, 0ffe6dcd).
2. ``pipeline/render/overlays/word_caption_pngs.py`` — caught
   ImportError on ``pipeline.captions.render_word_caption``, WARN-
   logged, and returned ``[]`` — shipped 8/10 mystoriesanimated
   shorts with ZERO captions in the 2026-05-15 canary batch (job
   f1e319a3).
3. ``pipeline/render/visualize/ai_beat_slideshow.py`` — per-beat
   image-gen failures hit ``except: continue`` silently; compose
   padded the missing frames with the last image, producing frozen-
   frame tails on sportsrecapped renders during cloud incidents.
4. ``pipeline/render/short_engine.py::_collect_overlays`` — caption
   producer failures were warn-and-skip even when
   ``spec.captions_enabled is True``.
5. ``pipeline/render/visualize/archival_shotlist.py`` +
   ``footage_windows.py`` — both fell back to solid color silently
   when their primary path failed.

The fix is uniform: each site now raises
:class:`pipeline.render.contracts.RenderFailedError` by default. The
single env override ``YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1`` re-
enables the legacy silent-degrade for emergency renders.

This module pins ONE test per audit site showing the bug condition
triggers ``raise RenderFailedError`` (and not a successful render
with a broken artifact), plus the override-flag fall-through.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from pipeline.render.contracts import RenderFailedError, Timeline, VisualTrack
from pipeline.render.visualize._fallback import (
    _fallback_to_longform_panels,
    _solid_color,
    _solid_color_override_enabled,
)


def _spec(extra: dict | None = None, **overrides) -> MagicMock:
    """Build a minimal spec mock that satisfies every fail-loud site."""
    s = MagicMock()
    s.output_resolution = (1080, 1920)
    s.output_fps = 30
    s.extra = extra or {}
    s.channel = "test"
    s.captions_enabled = overrides.get("captions_enabled", True)
    s.visual_mode = MagicMock()
    s.visual_mode.value = "ai_beat_slideshow"
    return s


def _timeline(end_s: float = 60.0) -> Timeline:
    seg = MagicMock()
    seg.start_s = 0.0
    seg.end_s = end_s
    seg.text = "scene text"
    seg.anchor_id = "anchor_001"
    return [seg]


# ---------------------------------------------------------------------------
# Site 1: _fallback.py::_solid_color
# ---------------------------------------------------------------------------


class SolidColorRaisesByDefaultTest(unittest.TestCase):
    """``_solid_color`` raises ``RenderFailedError`` unless the env
    override is set."""

    def test_raises_render_failed_error_by_default(self):
        spec = _spec()
        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch.dict(os.environ, {}, clear=False):
                # Ensure the env flag is NOT set.
                os.environ.pop("YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK", None)
                with self.assertRaises(RenderFailedError) as ctx:
                    _solid_color(
                        spec, _timeline(), wd,
                        color="0x141414", label="fail_loud_test",
                    )
        # Message must reference the file:line + the override hint.
        msg = str(ctx.exception)
        self.assertIn("_solid_color", msg)
        self.assertIn("YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK", msg)

    def test_env_override_produces_solid_color_mp4(self):
        spec = _spec()
        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch.dict(
                os.environ, {"YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK": "1"},
            ), patch(
                "pipeline.render.visualize._fallback.run_ffmpeg",
            ) as run_ffmpeg_mock, patch(
                "pipeline.render.visualize._fallback.probe_duration",
                return_value=60.0,
            ):
                track = _solid_color(
                    spec, _timeline(), wd,
                    color="0x141414", label="override_ok",
                )
        self.assertTrue(run_ffmpeg_mock.called)
        self.assertEqual(track.extras["source"], "override_ok")

    def test_render_failed_error_chains_original_cause(self):
        spec = _spec()
        cause = RuntimeError("simulated image-gen failure")
        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            os.environ.pop("YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK", None)
            with self.assertRaises(RenderFailedError) as ctx:
                _solid_color(
                    spec, _timeline(), wd,
                    color="0x141414", label="chain_test",
                    cause=cause,
                )
        # __cause__ chained via raise … from cause.
        self.assertIs(ctx.exception.__cause__, cause)

    def test_override_env_helper_accepts_truthy_values(self):
        for val in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict(
                os.environ, {"YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK": val},
            ):
                self.assertTrue(
                    _solid_color_override_enabled(),
                    f"env value {val!r} should enable override",
                )

    def test_override_env_helper_rejects_falsy_values(self):
        for val in ("", "0", "false", "no", "off"):
            with patch.dict(
                os.environ, {"YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK": val},
            ):
                self.assertFalse(
                    _solid_color_override_enabled(),
                    f"env value {val!r} should NOT enable override",
                )


# ---------------------------------------------------------------------------
# Site 2: overlays/word_caption_pngs.py
# ---------------------------------------------------------------------------


class WordCaptionsRaiseOnImportFailureWhenEnabledTest(unittest.TestCase):
    """Pin: when ``spec.captions_enabled is True`` and the caption
    helper can't be imported, the plugin RAISES. When False, returns
    [] silently."""

    def _build_audio_result(self, tmp: Path) -> MagicMock:
        ar = MagicMock()
        ar.narration_path = tmp / "narration.wav"
        ar.narration_path.parent.mkdir(parents=True, exist_ok=True)
        ar.narration_path.touch()
        return ar

    def _build_spec(self, captions_enabled: bool) -> MagicMock:
        spec = _spec(captions_enabled=captions_enabled)
        spec.output_resolution = (1080, 1920)
        spec.captions_density = MagicMock()
        spec.captions_density.value = "standard"
        spec.caption_style = MagicMock()
        spec.caption_style.font_size_minimal = 100
        spec.caption_style.font_size_standard = 80
        spec.caption_style.font_size_dense = 60
        return spec

    def test_raises_when_captions_enabled_and_import_fails(self):
        from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            spec = self._build_spec(captions_enabled=True)
            audio = self._build_audio_result(wd)
            timeline = _timeline()

            # Patch the import inside the plugin to raise ImportError.
            import builtins
            real_import = builtins.__import__

            def patched_import(name, *args, **kwargs):
                if name == "pipeline.captions":
                    raise ImportError("simulated Pillow missing")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=patched_import):
                with self.assertRaises(RenderFailedError) as ctx:
                    WordCaptionPngs().produce(spec, timeline, audio)

        msg = str(ctx.exception)
        self.assertIn("captions_enabled=True", msg)
        self.assertIn("word_caption_pngs", msg)

    def test_returns_empty_when_captions_disabled_and_import_fails(self):
        from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            spec = self._build_spec(captions_enabled=False)
            audio = self._build_audio_result(wd)
            timeline = _timeline()

            import builtins
            real_import = builtins.__import__

            def patched_import(name, *args, **kwargs):
                if name == "pipeline.captions":
                    raise ImportError("simulated Pillow missing")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=patched_import):
                elements = WordCaptionPngs().produce(spec, timeline, audio)

        self.assertEqual(elements, [],
            "captions_enabled=False MUST still return [] silently — "
            "user explicitly opted out, no caption regression possible.")


# ---------------------------------------------------------------------------
# Site 3: visualize/ai_beat_slideshow.py
# ---------------------------------------------------------------------------


class AiBeatSlideshowPerBeatFailureTest(unittest.TestCase):
    """Pin the 0-failures-OK and the >10%-failures-RAISES paths."""

    def _build_spec(self) -> MagicMock:
        spec = _spec()
        spec.output_resolution = (1080, 1920)
        spec.output_fps = 30
        spec.extra = {
            "image_provider": "cloudrun_z_image_turbo",
            "image_seed": 42,
            "image_steps": 4,
        }
        return spec

    def _build_timeline(self, n: int) -> Timeline:
        segs = []
        for i in range(n):
            seg = MagicMock()
            seg.start_s = float(i)
            seg.end_s = float(i + 1)
            seg.text = f"beat {i}"
            seg.anchor_id = f"beat_{i:03d}"
            segs.append(seg)
        return segs

    def test_zero_failures_succeeds(self):
        from pipeline.render.visualize.ai_beat_slideshow import AiBeatSlideshow

        spec = self._build_spec()
        timeline = self._build_timeline(20)

        # generate succeeds every time. _stitch_images creates the mp4.
        with TemporaryDirectory() as tmp:
            wd = Path(tmp)

            def fake_generate(out_path, **kwargs):
                Path(out_path).write_bytes(b"fake png")

            def fake_stitch(images, timeline, spec, out_path):
                Path(out_path).write_bytes(b"fake mp4")

            with patch(
                "pipeline.images.images.generate",
                side_effect=fake_generate,
            ), patch.object(
                AiBeatSlideshow, "_stitch_images", side_effect=fake_stitch,
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow.probe_duration",
                return_value=20.0,
            ):
                track = AiBeatSlideshow().produce(spec, timeline, wd)

        self.assertEqual(track.extras["source"], "ai_beat_slideshow")
        self.assertEqual(track.extras["n_images"], 20)

    def test_fifty_percent_failures_raises(self):
        from pipeline.render.visualize.ai_beat_slideshow import AiBeatSlideshow

        spec = self._build_spec()
        timeline = self._build_timeline(20)

        # Fail beats with even index → 50% failure rate.
        call_count = {"i": 0}

        def fake_generate(out_path, **kwargs):
            i = call_count["i"]
            call_count["i"] += 1
            if i % 2 == 0:
                raise RuntimeError(f"simulated beat {i} failure")
            Path(out_path).write_bytes(b"fake png")

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch(
                "pipeline.images.images.generate",
                side_effect=fake_generate,
            ):
                with self.assertRaises(RenderFailedError) as ctx:
                    AiBeatSlideshow().produce(spec, timeline, wd)

        msg = str(ctx.exception)
        self.assertIn("10%", msg)
        self.assertIn("ai_beat_slideshow", msg)
        # Should report 10/20 failures.
        self.assertIn("10/20", msg)

    def test_below_threshold_failures_continue(self):
        """5% failures (1/20) is under the 10% threshold — should not
        raise; produces a slideshow with 19 images instead of 20."""
        from pipeline.render.visualize.ai_beat_slideshow import AiBeatSlideshow

        spec = self._build_spec()
        timeline = self._build_timeline(20)

        call_count = {"i": 0}

        def fake_generate(out_path, **kwargs):
            i = call_count["i"]
            call_count["i"] += 1
            if i == 5:  # one failure out of 20 → 5%
                raise RuntimeError("simulated single beat failure")
            Path(out_path).write_bytes(b"fake png")

        def fake_stitch(images, timeline, spec, out_path):
            Path(out_path).write_bytes(b"fake mp4")

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch(
                "pipeline.images.images.generate",
                side_effect=fake_generate,
            ), patch.object(
                AiBeatSlideshow, "_stitch_images", side_effect=fake_stitch,
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow.probe_duration",
                return_value=20.0,
            ):
                track = AiBeatSlideshow().produce(spec, timeline, wd)

        self.assertEqual(track.extras["n_images"], 19,
            "1 failure out of 20 (5%) is under 10% threshold — "
            "should continue and produce 19 images.")


# ---------------------------------------------------------------------------
# Site 4: short_engine.py::_collect_overlays (captions branch)
# ---------------------------------------------------------------------------


class CollectOverlaysCaptionsFailLoudTest(unittest.TestCase):
    """Pin: when captions_enabled is True and the caption producer
    raises, ``_collect_overlays`` MUST propagate as
    ``RenderFailedError`` (not warn + return)."""

    def test_captions_enabled_with_producer_failure_raises(self):
        from pipeline.render.short_engine import _collect_overlays

        spec = _spec(captions_enabled=True)
        spec.captions_enabled = True
        spec.lower_thirds = False
        spec.chapter_cards = False
        spec.overlay_timeline = False
        spec.captions_layout = MagicMock()
        spec.captions_layout = type("L", (), {"value": "word"})()
        # Bypass the enum compare in _captions_plugin_for_layout —
        # the producer get_plugin will be patched anyway.

        audio = MagicMock()
        timeline = _timeline()

        with patch(
            "pipeline.render.short_engine.get_plugin",
        ) as gp, patch(
            "pipeline.render.short_engine._captions_plugin_for_layout",
            return_value="word_caption_pngs",
        ):
            failing_plugin = MagicMock()
            failing_plugin.produce.side_effect = RuntimeError(
                "simulated caption render failure"
            )
            gp.return_value = failing_plugin

            with self.assertRaises(RenderFailedError) as ctx:
                _collect_overlays(spec, timeline, audio)

        msg = str(ctx.exception)
        self.assertIn("captions_enabled=True", msg)
        self.assertIn("word_caption_pngs", msg)

    def test_captions_enabled_with_render_failed_error_propagates(self):
        """If the producer ALREADY raises RenderFailedError (e.g.
        word_caption_pngs detected captions_enabled=True + Pillow
        missing), the engine MUST NOT wrap it — propagate as-is so
        the original site is visible in the traceback."""
        from pipeline.render.short_engine import _collect_overlays

        spec = _spec(captions_enabled=True)
        spec.captions_enabled = True
        spec.lower_thirds = False
        spec.chapter_cards = False
        spec.overlay_timeline = False

        audio = MagicMock()
        timeline = _timeline()

        original = RenderFailedError("upstream fail-loud signal")

        with patch(
            "pipeline.render.short_engine.get_plugin",
        ) as gp, patch(
            "pipeline.render.short_engine._captions_plugin_for_layout",
            return_value="word_caption_pngs",
        ):
            failing_plugin = MagicMock()
            failing_plugin.produce.side_effect = original
            gp.return_value = failing_plugin

            with self.assertRaises(RenderFailedError) as ctx:
                _collect_overlays(spec, timeline, audio)

        # Same instance — engine didn't wrap.
        self.assertIs(ctx.exception, original)

    def test_captions_disabled_with_producer_failure_does_not_raise(self):
        """captions_enabled=False MUST skip the captions branch
        entirely — no get_plugin call, no raise."""
        from pipeline.render.short_engine import _collect_overlays

        spec = _spec(captions_enabled=False)
        spec.captions_enabled = False
        spec.lower_thirds = False
        spec.chapter_cards = False
        spec.overlay_timeline = False

        audio = MagicMock()
        timeline = _timeline()

        with patch(
            "pipeline.render.short_engine.get_plugin",
        ) as gp:
            out = _collect_overlays(spec, timeline, audio)

        self.assertEqual(out, [])
        gp.assert_not_called()

    def test_optin_overlay_failure_still_warn_and_skip(self):
        """Lower-thirds / chapter cards / anchored footage are OPT-IN
        flags. Their failures keep the warn-and-skip behavior per the
        audit rule."""
        from pipeline.render.short_engine import _collect_overlays

        spec = _spec(captions_enabled=False)
        spec.captions_enabled = False
        spec.lower_thirds = True  # opt-in
        spec.chapter_cards = True  # opt-in
        spec.overlay_timeline = False

        audio = MagicMock()
        timeline = _timeline()

        failing_plugin = MagicMock()
        failing_plugin.produce.side_effect = RuntimeError("simulated failure")

        with patch(
            "pipeline.render.short_engine.get_plugin",
            return_value=failing_plugin,
        ):
            # MUST NOT raise — opt-in overlays warn-and-skip.
            out = _collect_overlays(spec, timeline, audio)

        self.assertEqual(out, [],
            "opt-in overlays failing should warn-and-skip, not raise")


# ---------------------------------------------------------------------------
# Site 5: archival_shotlist + footage_windows fallback paths
# ---------------------------------------------------------------------------


class ArchivalShotlistFallbackRaisesWhenLongformPanelsFailsTest(unittest.TestCase):
    """archival_shotlist has no shotlist_path → dispatches to
    longform_panels. If longform_panels also fails AND override env
    is not set, the whole chain MUST raise RenderFailedError."""

    def test_chain_raises_when_panels_fail_and_no_override(self):
        from pipeline.render.visualize.archival_shotlist import ArchivalShotlist

        spec = _spec()  # no shotlist_path in extra
        timeline = _timeline()

        plugin_mock = MagicMock()
        plugin_mock.produce.side_effect = RuntimeError("image-gen down")

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            os.environ.pop("YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK", None)
            with patch(
                "pipeline.render.visualize._fallback.get_plugin",
                return_value=plugin_mock,
            ):
                with self.assertRaises(RenderFailedError):
                    ArchivalShotlist().produce(spec, timeline, wd)

    def test_chain_returns_solid_color_when_override_set(self):
        from pipeline.render.visualize.archival_shotlist import ArchivalShotlist

        spec = _spec()
        timeline = _timeline()

        plugin_mock = MagicMock()
        plugin_mock.produce.side_effect = RuntimeError("image-gen down")

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch.dict(
                os.environ, {"YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK": "1"},
            ), patch(
                "pipeline.render.visualize._fallback.get_plugin",
                return_value=plugin_mock,
            ), patch(
                "pipeline.render.visualize._fallback.run_ffmpeg",
            ), patch(
                "pipeline.render.visualize._fallback.probe_duration",
                return_value=60.0,
            ):
                track = ArchivalShotlist().produce(spec, timeline, wd)
        # Override path returns solid color.
        self.assertEqual(
            track.extras["source"], "archival_shotlist_fallback"
        )


class FootageWindowsFallbackRaisesWhenLongformPanelsFailsTest(unittest.TestCase):
    """Same as ArchivalShotlist — footage_windows fallback chain MUST
    raise when longform_panels fails and override is not set."""

    def test_chain_raises_when_panels_fail_and_no_override(self):
        from pipeline.render.visualize.footage_windows import FootageWindows

        spec = _spec()  # no shotlist_path
        timeline = _timeline()

        plugin_mock = MagicMock()
        plugin_mock.produce.side_effect = RuntimeError("image-gen down")

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            os.environ.pop("YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK", None)
            with patch(
                "pipeline.render.visualize._fallback.get_plugin",
                return_value=plugin_mock,
            ):
                with self.assertRaises(RenderFailedError):
                    FootageWindows().produce(spec, timeline, wd)


# ---------------------------------------------------------------------------
# Exception-class contract
# ---------------------------------------------------------------------------


class RenderFailedErrorContractTest(unittest.TestCase):
    """Pin the exception class itself — it's a subclass of
    RuntimeError so callers that don't know about it still catch it
    via ``except Exception``."""

    def test_is_subclass_of_runtime_error(self):
        self.assertTrue(issubclass(RenderFailedError, RuntimeError))

    def test_is_subclass_of_exception(self):
        self.assertTrue(issubclass(RenderFailedError, Exception))

    def test_carries_message(self):
        e = RenderFailedError("hello")
        self.assertEqual(str(e), "hello")

    def test_chains_via_from(self):
        original = ValueError("inner")
        try:
            try:
                raise original
            except ValueError as ve:
                raise RenderFailedError("outer") from ve
        except RenderFailedError as e:
            self.assertIs(e.__cause__, original)


if __name__ == "__main__":
    unittest.main()
